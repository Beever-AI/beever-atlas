"""Bring up the public tunnel and keep inbound webhooks (Slack Events API,
Microsoft Teams) reachable across restarts — the reboot-proofing for platforms
that can't use an outbound transport.

Discord, Mattermost, and Slack Socket Mode connect outbound and need none of
this. Teams (Bot Framework) has no Socket Mode equivalent, so it always needs a
public messaging endpoint — this script automates pointing it at the tunnel.

What it does (idempotent):
  1. Start ngrok on the bot port (static domain if NGROK_DOMAIN is set, else an
     ephemeral URL). If an ngrok agent is already running, reuse it.
  2. Read the public https URL from ngrok's local API (127.0.0.1:4040).
  3. Write PUBLIC_BOT_URL into .env (so the backend's /api/config/connectivity
     shows the right webhook URLs).
  4. Restart the backend container so it picks up the new PUBLIC_BOT_URL.
  5. Re-point the Teams messaging endpoint to <url>/api/teams (needs the Teams
     CLI logged in + TEAMS_APP_ID set).
  6. Stay alive as ngrok's parent. If ngrok exits, this exits too — so a launchd
     KeepAlive agent restarts the whole thing and re-syncs after a reboot.

With a STATIC domain the URL never changes, so steps 3-5 are effectively
one-time and reboots "just work". With an ephemeral URL they re-run every start.

Usage:
    uv run python -m scripts.tunnel_up                 # start + sync + hold
    uv run python -m scripts.tunnel_up --dry-run       # print plan, change nothing
    uv run python -m scripts.tunnel_up --no-restart    # skip docker restart
    uv run python -m scripts.tunnel_up --no-teams      # skip Teams re-point
    make tunnel-up [NGROK_DOMAIN=your-name.ngrok-free.app]

Config (CLI flag > env var > .env):
    NGROK_DOMAIN   reserved static ngrok domain (recommended; free tier has one)
    TEAMS_APP_ID   Teams app id from `teams app list` (the messaging-endpoint owner)
    BOT_PORT       local bot port (default 3001)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
NGROK_API = "http://127.0.0.1:4040/api/tunnels"


def _log(msg: str) -> None:
    print(f"[tunnel_up] {msg}", flush=True)


def _read_env_file() -> dict[str, str]:
    """Parse simple KEY=VALUE lines from .env (best-effort, ignores comments)."""
    values: dict[str, str] = {}
    if not ENV_PATH.exists():
        return values
    for line in ENV_PATH.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        values[key.strip()] = val.strip()
    return values


def _cfg(name: str, cli_value: str | None, env_file: dict[str, str]) -> str:
    """Resolve config: CLI flag > process env > .env file > ''."""
    if cli_value:
        return cli_value
    return os.environ.get(name) or env_file.get(name, "")


def _get_ngrok_url() -> str | None:
    """Return the https public_url from a running ngrok agent, or None."""
    try:
        with urllib.request.urlopen(NGROK_API, timeout=3) as resp:  # noqa: S310 (localhost)
            data = json.load(resp)
    except Exception:
        return None
    for tunnel in data.get("tunnels", []):
        url = tunnel.get("public_url", "")
        if url.startswith("https://"):
            return url
    return None


def _start_ngrok(port: str, domain: str) -> subprocess.Popen | None:
    """Start ngrok as a child process. Returns the Popen, or None on dry-run."""
    cmd = ["ngrok", "http", port, "--log=stdout"]
    if domain:
        cmd.append(f"--url=https://{domain}")
    _log(f"starting: {' '.join(cmd)}")
    return subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _wait_for_url(timeout_s: int = 30) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        url = _get_ngrok_url()
        if url:
            return url
        time.sleep(1)
    return None


def _write_public_bot_url(url: str, dry_run: bool) -> None:
    """Upsert PUBLIC_BOT_URL=<url> in .env, preserving the rest of the file."""
    if dry_run:
        _log(f"DRY-RUN would set PUBLIC_BOT_URL={url} in {ENV_PATH}")
        return
    lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    out, replaced = [], False
    for line in lines:
        if line.strip().startswith("PUBLIC_BOT_URL="):
            out.append(f"PUBLIC_BOT_URL={url}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"PUBLIC_BOT_URL={url}")
    ENV_PATH.write_text("\n".join(out) + "\n")
    _log(f"set PUBLIC_BOT_URL={url}")


def _restart_backend(dry_run: bool) -> None:
    cmd = ["docker", "compose", "restart", "beever-atlas"]
    if dry_run:
        _log(f"DRY-RUN would run: {' '.join(cmd)}")
        return
    if not shutil.which("docker"):
        _log("docker not found; skipping backend restart")
        return
    _log("restarting backend container so it serves the new PUBLIC_BOT_URL…")
    subprocess.run(cmd, cwd=REPO_ROOT, check=False)


def _update_teams_endpoint(app_id: str, url: str, dry_run: bool) -> None:
    if not app_id:
        _log(
            "TEAMS_APP_ID not set; skipping Teams endpoint update "
            "(set it to the id from `teams app list` to auto re-point Teams)"
        )
        return
    endpoint = f"{url}/api/teams"
    cmd = ["teams", "app", "update", app_id, "--endpoint", endpoint, "-y"]
    if dry_run:
        _log(f"DRY-RUN would run: {' '.join(cmd)}")
        return
    if not shutil.which("teams"):
        _log(
            "teams CLI not found; skipping Teams endpoint update "
            "(install it + `teams login`, or re-point manually)"
        )
        return
    _log(f"pointing Teams messaging endpoint at {endpoint}…")
    subprocess.run(cmd, check=False)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--domain", default=None, help="static ngrok domain (overrides NGROK_DOMAIN)"
    )
    parser.add_argument(
        "--teams-app-id", default=None, help="Teams app id (overrides TEAMS_APP_ID)"
    )
    parser.add_argument("--port", default=None, help="bot port (overrides BOT_PORT, default 3001)")
    parser.add_argument(
        "--no-restart", action="store_true", help="don't restart the backend container"
    )
    parser.add_argument("--no-teams", action="store_true", help="don't re-point the Teams endpoint")
    parser.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    args = parser.parse_args()

    env_file = _read_env_file()
    domain = _cfg("NGROK_DOMAIN", args.domain, env_file)
    teams_app_id = _cfg("TEAMS_APP_ID", args.teams_app_id, env_file)
    port = _cfg("BOT_PORT", args.port, env_file) or "3001"

    if not shutil.which("ngrok") and not args.dry_run:
        _log("ERROR: ngrok not installed (brew install ngrok)")
        return 1

    _log(f"port={port} domain={domain or '(ephemeral)'} teams_app_id={teams_app_id or '(unset)'}")
    if not domain:
        _log(
            "NOTE: no NGROK_DOMAIN — using an ephemeral URL that changes each "
            "start. Reserve a free static domain (dashboard.ngrok.com/domains) "
            "so reboots stop changing the URL."
        )

    # Dry-run: compute the URL we'd use without touching anything.
    if args.dry_run:
        url = (
            f"https://{domain}"
            if domain
            else (_get_ngrok_url() or "https://<ephemeral>.ngrok-free.app")
        )
        _log(f"DRY-RUN plan for url={url}")
        _write_public_bot_url(url, dry_run=True)
        if not args.no_restart:
            _restart_backend(dry_run=True)
        if not args.no_teams:
            _update_teams_endpoint(teams_app_id, url, dry_run=True)
        _log("DRY-RUN complete (nothing changed).")
        return 0

    # Reuse a running agent, else start our own child.
    child: subprocess.Popen | None = None
    url = _get_ngrok_url()
    if url:
        _log(f"reusing running ngrok agent at {url}")
    else:
        child = _start_ngrok(port, domain)
        url = _wait_for_url()
        if not url:
            _log("ERROR: ngrok did not report a public URL within timeout")
            if child:
                child.terminate()
            return 1
        _log(f"tunnel up at {url}")

    # Sync everything that depends on the public URL.
    _write_public_bot_url(url, dry_run=False)
    if not args.no_restart:
        _restart_backend(dry_run=False)
    if not args.no_teams:
        _update_teams_endpoint(teams_app_id, url, dry_run=False)

    if child is None:
        _log("ngrok was already running; sync done. Exiting (not holding a child).")
        return 0

    # Hold ngrok open. On exit (or ngrok death) a launchd KeepAlive agent
    # restarts us and re-syncs. Forward SIGTERM/SIGINT to ngrok for clean stop.
    def _terminate(_signum, _frame):
        _log("received stop signal; terminating ngrok…")
        child.terminate()

    signal.signal(signal.SIGTERM, _terminate)
    signal.signal(signal.SIGINT, _terminate)
    _log("holding tunnel open (Ctrl-C to stop). Slack(Events)/Teams now reachable.")
    rc = child.wait()
    _log(f"ngrok exited with code {rc}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
