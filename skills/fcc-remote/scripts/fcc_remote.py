#!/usr/bin/env python3
"""Detect and use a remote Free Claude Code server through an SSH tunnel."""

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

DEFAULT_HOST = "fcc-host"
DEFAULT_LOCAL_PORT = 18082
DEFAULT_REMOTE_PORT = 8082
DEFAULT_TOKEN = "freecc"
CONNECT_TIMEOUT_SECONDS = 8
HEALTH_WAIT_SECONDS = 10

DIRECT_OPENER = build_opener(ProxyHandler({}))


class RemoteFccError(RuntimeError):
    """A user-actionable remote FCC failure."""


def parse_port(raw: str) -> int:
    """Parse a TCP port accepted by argparse."""

    try:
        port = int(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line interface."""

    parser = argparse.ArgumentParser(
        description="Connect Claude Code to a remote FCC/Ollama host over SSH."
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("FCC_REMOTE_SSH_HOST", DEFAULT_HOST),
        help="SSH destination or alias (default: %(default)s)",
    )
    parser.add_argument(
        "--local-port",
        type=parse_port,
        default=parse_port(
            os.environ.get("FCC_REMOTE_LOCAL_PORT", str(DEFAULT_LOCAL_PORT))
        ),
        help="client loopback port (default: %(default)s)",
    )
    parser.add_argument(
        "--remote-port",
        type=parse_port,
        default=parse_port(os.environ.get("FCC_REMOTE_PORT", str(DEFAULT_REMOTE_PORT))),
        help="remote FCC loopback port (default: %(default)s)",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("FCC_REMOTE_TOKEN", DEFAULT_TOKEN),
        help="FCC bearer token; prefer FCC_REMOTE_TOKEN",
    )
    subparsers = parser.add_subparsers(dest="action", required=True)
    subparsers.add_parser("doctor", help="check client prerequisites")
    subparsers.add_parser("probe", help="prove the remote FCC health endpoint")
    subparsers.add_parser("connect", help="start the SSH tunnel and list models")
    subparsers.add_parser("status", help="check the local tunnel and list models")
    subparsers.add_parser("disconnect", help="close this skill's SSH tunnel")
    claude = subparsers.add_parser("claude", help="connect and launch Claude Code")
    claude.add_argument("claude_args", nargs=argparse.REMAINDER)
    return parser


def command_path(name: str) -> str:
    """Return one required executable path or raise a useful failure."""

    path = shutil.which(name)
    if path is None:
        raise RemoteFccError(f"Required command is missing: {name}")
    return path


def state_dir() -> Path:
    """Return the private client state directory."""

    path = Path.home() / ".fcc-remote"
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def control_socket(host: str, local_port: int, remote_port: int) -> Path:
    """Return a short deterministic SSH control-socket path."""

    identity = f"{host}:{local_port}:{remote_port}".encode()
    digest = hashlib.sha256(identity).hexdigest()[:16]
    return state_dir() / f"ssh-{digest}.sock"


def base_url(local_port: int) -> str:
    """Return the tunneled FCC base URL."""

    return f"http://127.0.0.1:{local_port}"


def request_json(url: str, *, token: str | None = None) -> Any:
    """Read one local endpoint without consulting machine proxy settings."""

    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, headers=headers)
    try:
        with DIRECT_OPENER.open(request, timeout=5) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        raise RemoteFccError(f"{url} returned HTTP {exc.code}") from exc
    except URLError as exc:
        raise RemoteFccError(f"Cannot reach {url}: {exc.reason}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise RemoteFccError(f"Invalid response from {url}: {exc}") from exc


def local_health(local_port: int) -> dict[str, Any]:
    """Return a validated tunneled health response."""

    payload = request_json(f"{base_url(local_port)}/health")
    if not isinstance(payload, dict) or payload.get("status") != "healthy":
        raise RemoteFccError(f"Unexpected FCC health response: {payload!r}")
    return payload


def remote_probe(host: str, remote_port: int) -> dict[str, Any]:
    """Read FCC health on the remote loopback interface through SSH."""

    ssh = command_path("ssh")
    remote_command = f"curl -fsS --max-time 5 http://127.0.0.1:{remote_port}/health"
    completed = subprocess.run(
        [
            ssh,
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={CONNECT_TIMEOUT_SECONDS}",
            host,
            remote_command,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RemoteFccError(
            f"Remote FCC probe failed for {host}: {detail or 'unknown SSH error'}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteFccError(
            f"Remote host did not return FCC health JSON: {completed.stdout!r}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("status") != "healthy":
        raise RemoteFccError(f"Remote FCC is not healthy: {payload!r}")
    return payload


def socket_is_open(local_port: int) -> bool:
    """Return whether a TCP listener already owns the requested local port."""

    with socket.socket() as client:
        client.settimeout(0.3)
        return client.connect_ex(("127.0.0.1", local_port)) == 0


def control_master_is_live(socket_path: Path, host: str) -> bool:
    """Return whether the skill-owned SSH control master is active."""

    if not socket_path.exists():
        return False
    completed = subprocess.run(
        [command_path("ssh"), "-S", str(socket_path), "-O", "check", host],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def start_tunnel(host: str, local_port: int, remote_port: int) -> None:
    """Start or reuse a private SSH local-forwarding control master."""

    try:
        local_health(local_port)
        print(f"FCC tunnel already healthy: {base_url(local_port)}")
        return
    except RemoteFccError:
        pass

    socket_path = control_socket(host, local_port, remote_port)
    if control_master_is_live(socket_path, host):
        raise RemoteFccError(
            "SSH control master exists but FCC is unhealthy; run disconnect first"
        )
    socket_path.unlink(missing_ok=True)
    if socket_is_open(local_port):
        raise RemoteFccError(
            f"Local port {local_port} is already occupied; choose --local-port"
        )

    remote_probe(host, remote_port)
    completed = subprocess.run(
        [
            command_path("ssh"),
            "-fN",
            "-M",
            "-S",
            str(socket_path),
            "-o",
            "BatchMode=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=3",
            "-L",
            f"127.0.0.1:{local_port}:127.0.0.1:{remote_port}",
            host,
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RemoteFccError(f"Could not establish SSH tunnel: {detail}")

    deadline = time.monotonic() + HEALTH_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            local_health(local_port)
            print(f"FCC tunnel ready: {base_url(local_port)}")
            return
        except RemoteFccError:
            time.sleep(0.2)
    raise RemoteFccError("SSH tunnel started but FCC did not become healthy")


def model_ids(local_port: int, token: str) -> list[str]:
    """Return advertised model IDs through the tunnel."""

    payload = request_json(f"{base_url(local_port)}/v1/models", token=token)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise RemoteFccError(f"Unexpected FCC model response: {payload!r}")
    return [
        entry["id"]
        for entry in payload["data"]
        if isinstance(entry, dict) and isinstance(entry.get("id"), str)
    ]


def print_status(local_port: int, token: str) -> None:
    """Print health and advertised models without exposing the token."""

    local_health(local_port)
    print(f"Health: healthy ({base_url(local_port)})")
    models = model_ids(local_port, token)
    print(f"Models: {len(models)}")
    for model in models:
        print(f"- {model}")


def disconnect(host: str, local_port: int, remote_port: int) -> None:
    """Close only the control master created for this connection."""

    socket_path = control_socket(host, local_port, remote_port)
    if not socket_path.exists():
        print("No skill-owned FCC tunnel is recorded.")
        return
    completed = subprocess.run(
        [command_path("ssh"), "-S", str(socket_path), "-O", "exit", host],
        check=False,
        capture_output=True,
        text=True,
    )
    socket_path.unlink(missing_ok=True)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise RemoteFccError(f"Could not close SSH tunnel cleanly: {detail}")
    print("FCC tunnel closed.")


def no_proxy_value(env: dict[str, str]) -> str:
    """Return NO_PROXY with loopback entries included once."""

    values: list[str] = []
    seen: set[str] = set()
    for key in ("NO_PROXY", "no_proxy"):
        for raw in env.get(key, "").split(","):
            value = raw.strip()
            normalized = value.casefold()
            if value and normalized not in seen:
                seen.add(normalized)
                values.append(value)
    for value in ("127.0.0.1", "localhost", "::1"):
        if value.casefold() not in seen:
            seen.add(value.casefold())
            values.append(value)
    return ",".join(values)


def run_claude(local_port: int, token: str, claude_args: list[str]) -> int:
    """Launch Claude Code with an isolated FCC settings override."""

    claude = command_path("claude")
    url = base_url(local_port)
    settings = {
        "env": {
            "ANTHROPIC_AUTH_TOKEN": token or "fcc-no-auth",
            "ANTHROPIC_BASE_URL": url,
            "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "190000",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
        }
    }
    descriptor, raw_path = tempfile.mkstemp(prefix="fcc-remote-", suffix=".json")
    settings_path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            json.dump(settings, file)
        settings_path.chmod(0o600)

        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("ANTHROPIC_")
            and key != "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
        }
        env["ANTHROPIC_AUTH_TOKEN"] = token or "fcc-no-auth"
        env["ANTHROPIC_BASE_URL"] = url
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = "190000"
        env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
        env["DISABLE_AUTOUPDATER"] = "1"
        env["DISABLE_FEEDBACK_COMMAND"] = "1"
        env["DISABLE_ERROR_REPORTING"] = "1"
        bypass = no_proxy_value(env)
        env["NO_PROXY"] = bypass
        env["no_proxy"] = bypass

        args = claude_args[1:] if claude_args[:1] == ["--"] else claude_args
        completed = subprocess.run(
            [claude, "--settings", str(settings_path), *args],
            check=False,
            env=env,
        )
        return completed.returncode
    finally:
        settings_path.unlink(missing_ok=True)


def doctor() -> None:
    """Print client prerequisites without making a connection."""

    print(f"ssh: {command_path('ssh')}")
    print(f"python: {sys.executable}")
    claude = shutil.which("claude")
    print(f"claude: {claude or 'missing (required only for claude action)'}")


def main() -> int:
    """Execute one remote FCC action."""

    args = build_parser().parse_args()
    try:
        if args.action == "doctor":
            doctor()
        elif args.action == "probe":
            remote_probe(args.host, args.remote_port)
            print(f"Remote FCC healthy: {args.host}:127.0.0.1:{args.remote_port}")
        elif args.action == "connect":
            start_tunnel(args.host, args.local_port, args.remote_port)
            print_status(args.local_port, args.token)
        elif args.action == "status":
            print_status(args.local_port, args.token)
        elif args.action == "disconnect":
            disconnect(args.host, args.local_port, args.remote_port)
        elif args.action == "claude":
            start_tunnel(args.host, args.local_port, args.remote_port)
            return run_claude(args.local_port, args.token, args.claude_args)
        else:
            raise RemoteFccError(f"Unsupported action: {args.action}")
    except RemoteFccError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
