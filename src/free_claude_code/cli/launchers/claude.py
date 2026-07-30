"""Installed `fcc-claude` launcher."""

import json
import os
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from free_claude_code.cli.claude_env import (
    CLAUDE_BINARY_NAME,
    build_claude_proxy_env,
    build_claude_proxy_settings,
)
from free_claude_code.config.server_urls import local_proxy_root_url
from free_claude_code.config.settings import get_settings

from .common import preflight_proxy, resolve_client_binary, run_client_process

_DISPLAY_NAME = "Claude Code"
_INSTALL_HINT = "Install Claude Code with: npm install -g @anthropic-ai/claude-code"


def launch(argv: Sequence[str] | None = None) -> None:
    """Launch Claude Code with Free Claude Code proxy environment variables."""

    settings = get_settings()
    proxy_root_url = local_proxy_root_url(settings)
    if error := preflight_proxy(proxy_root_url):
        print(
            f"Free Claude Code proxy is not reachable at {proxy_root_url}: {error}",
            file=sys.stderr,
        )
        print("Start it in another terminal with: fcc-server", file=sys.stderr)
        raise SystemExit(1)

    binary_name = claude_binary_name()
    binary_path = resolve_client_binary(
        binary_name=binary_name,
        display_name=_DISPLAY_NAME,
        install_hint=_INSTALL_HINT,
    )
    args = list(sys.argv[1:] if argv is None else argv)
    settings_path = write_claude_proxy_settings(
        proxy_root_url=proxy_root_url,
        auth_token=settings.anthropic_auth_token,
    )
    try:
        run_client_process(
            command=build_claude_launcher_command(
                binary_path=binary_path,
                argv=args,
                settings_path=settings_path,
            ),
            env=build_claude_proxy_env(
                proxy_root_url=proxy_root_url,
                auth_token=settings.anthropic_auth_token,
                base_env=os.environ,
            ),
            binary_name=binary_name,
            display_name=_DISPLAY_NAME,
            install_hint=_INSTALL_HINT,
        )
    finally:
        settings_path.unlink(missing_ok=True)


def claude_binary_name() -> str:
    """Return the Claude Code binary name."""

    return CLAUDE_BINARY_NAME


def build_claude_launcher_command(
    *, binary_path: str, argv: Sequence[str], settings_path: Path
) -> list[str]:
    """Return the Claude wrapper command without changing user arguments."""

    return [binary_path, "--settings", str(settings_path), *argv]


def write_claude_proxy_settings(*, proxy_root_url: str, auth_token: str) -> Path:
    """Write one private settings override for the lifetime of a Claude process."""

    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="fcc-claude-",
        suffix=".json",
        delete=False,
    ) as file:
        json.dump(
            build_claude_proxy_settings(
                proxy_root_url=proxy_root_url,
                auth_token=auth_token,
            ),
            file,
        )
        return Path(file.name)
