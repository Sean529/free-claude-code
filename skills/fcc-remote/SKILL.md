---
name: fcc-remote
description: Securely detect and use a Free Claude Code (FCC) server running on another computer through an SSH tunnel. Use when the user asks whether a remote computer supports FCC, wants another Mac/Windows/Linux machine to use that computer's Ollama models, mentions remote FCC or remote Claude Code, or needs to probe, connect, list models, launch Claude Code, diagnose, or disconnect an FCC SSH tunnel.
---

# Remote FCC

Use the bundled script to prove capability from live endpoints rather than trusting a static machine label. A remote host supports FCC only when its loopback `/health` endpoint succeeds; after tunneling, `/v1/models` proves which models are available.

## Prerequisites

- Require `ssh`, `curl` on the FCC host, and Python 3.10+ on the client.
- Require Claude Code only for the `claude` action.
- Use an SSH host or alias supplied by the user. Default to `fcc-host` when none is supplied.
- Never expose FCC port 8082 or Ollama port 11434 publicly. Keep both loopback-only and use SSH forwarding.
- Do not write passwords, private keys, or bearer tokens into this skill. Read the FCC token from `FCC_REMOTE_TOKEN`; default to `freecc` only for an unchanged local FCC installation.

## First-time SSH setup

If `ssh fcc-host` already works, skip setup. Otherwise obtain the remote SSH username and LAN/VPN hostname from the user, then create an alias in `~/.ssh/config` only when authorized:

```sshconfig
Host fcc-host
    HostName <remote-hostname-or-ip>
    User <remote-user>
    IdentityFile ~/.ssh/id_ed25519
```

Prefer a stable Tailscale hostname or LAN DNS name over a changing IP. Do not enable public port forwarding.

## Run the workflow

Resolve this skill directory and run `scripts/fcc_remote.py` with `uv run` when `uv` is available; otherwise use `python3`.

1. Check local prerequisites:

   ```bash
   uv run <skill-dir>/scripts/fcc_remote.py doctor
   ```

2. Prove the remote host is running FCC:

   ```bash
   uv run <skill-dir>/scripts/fcc_remote.py --host fcc-host probe
   ```

3. Establish the encrypted tunnel and list models:

   ```bash
   FCC_REMOTE_TOKEN=freecc uv run <skill-dir>/scripts/fcc_remote.py --host fcc-host connect
   ```

4. Launch Claude Code on the client while inference runs on the remote host:

   ```bash
   FCC_REMOTE_TOKEN=freecc uv run <skill-dir>/scripts/fcc_remote.py --host fcc-host claude
   ```

   Pass Claude arguments after `--`:

   ```bash
   FCC_REMOTE_TOKEN=freecc uv run <skill-dir>/scripts/fcc_remote.py --host fcc-host claude -- -p "Summarize this repository"
   ```

5. Inspect or close the tunnel:

   ```bash
   uv run <skill-dir>/scripts/fcc_remote.py --host fcc-host status
   uv run <skill-dir>/scripts/fcc_remote.py --host fcc-host disconnect
   ```

## Configuration

Accept command flags or equivalent environment variables:

| Purpose | Flag | Environment | Default |
| --- | --- | --- | --- |
| SSH destination | `--host` | `FCC_REMOTE_SSH_HOST` | `fcc-host` |
| Client loopback port | `--local-port` | `FCC_REMOTE_LOCAL_PORT` | `18082` |
| Remote FCC port | `--remote-port` | `FCC_REMOTE_PORT` | `8082` |
| FCC bearer token | `--token` | `FCC_REMOTE_TOKEN` | `freecc` |

Prefer environment variables for tokens so they do not appear in shell history. If remote auth fails, verify the token on the host in `~/.fcc/.env` without printing it.

## Interpret failures

- SSH failure: verify `ssh <host>` and the alias, VPN/LAN reachability, and Remote Login on the FCC host.
- Remote health failure: start `fcc-server` on the host and keep FCC bound to loopback.
- Tunnel failure: choose another `--local-port`; do not kill an unknown process occupying the port.
- Model-list 401/403: correct `FCC_REMOTE_TOKEN`.
- Claude still reaches another provider: always use the script's `claude` action; it supplies a private temporary settings override and removes it after exit.
- Ollama model errors: diagnose on the FCC host with `ollama list`, FCC logs, and a direct local request before changing client settings.

Report the SSH alias, local tunnel URL, health result, advertised model IDs, and exact launch command. Redact the token.
