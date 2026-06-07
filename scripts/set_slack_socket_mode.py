"""Migrate an existing Slack connection to Socket Mode IN PLACE — no delete.

The web UI has no edit-credentials flow, and deleting a connection cascades a
hard-purge of its sole-owned channels (messages/facts/wiki). This script adds an
app-level token (``xapp-...``) to the stored credentials of an existing Slack
connection so the bot switches to Socket Mode (outbound WebSocket, no public
URL / tunnel) on its next rebuild — preserving all synced data and channel
selections.

Usage (inside the backend container, which holds CREDENTIAL_MASTER_KEY + DB):

    docker compose exec beever-atlas \
        python -m scripts.set_slack_socket_mode --app-token xapp-XXXX

The token is read from --app-token or the SLACK_APP_TOKEN env var, so it can stay
on your machine and never has to be pasted into a chat. Pass --connection-id to
target a specific connection; otherwise the single Slack connection is used.

After it runs, restart the bot so it re-registers the adapter in socket mode:

    docker compose restart bot
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

from beever_atlas.infra.config import get_settings
from beever_atlas.stores import StoreClients, init_stores


def _redact(token: str) -> str:
    if len(token) <= 8:
        return "****"
    return f"{token[:7]}…{token[-4:]}"


async def _run(connection_id: str | None, app_token: str) -> int:
    settings = get_settings()
    stores = StoreClients.from_settings(settings)
    await stores.startup()
    init_stores(stores)

    conns = await stores.platform.list_connections()
    slack = [c for c in conns if c.platform == "slack"]
    if connection_id:
        slack = [c for c in slack if c.id == connection_id]

    if not slack:
        print("ERROR: no matching Slack connection found.", file=sys.stderr)
        return 1
    if len(slack) > 1:
        ids = ", ".join(c.id for c in slack)
        print(
            f"ERROR: multiple Slack connections ({ids}); pass --connection-id.",
            file=sys.stderr,
        )
        return 1

    conn = slack[0]
    creds = stores.platform.decrypt_connection_credentials(conn)
    before = sorted(creds.keys())

    # Merge — keep existing bot_token/signing_secret. The bot's bridge camelizes
    # stored snake_case keys (app_token -> appToken) before building the adapter,
    # and planSlackAdapter() prefers socket mode whenever appToken is present, so
    # signing_secret can safely stay (useful if you ever revert to Events API).
    # Match the stored convention: snake_case if that's what's already there.
    token_key = "app_token" if "bot_token" in creds else "appToken"
    creds[token_key] = app_token

    updated = await stores.platform.update_connection(conn.id, credentials=creds)
    if updated is None:
        print(f"ERROR: update failed for connection {conn.id}.", file=sys.stderr)
        return 1

    print(f"OK: connection {conn.id} ({conn.display_name})")
    print(f"  credentials before: {before}")
    print(f"  credentials after:  {sorted(creds.keys())}")
    print(f"  appToken set to:    {_redact(app_token)}")
    print("Next: `docker compose restart bot`, then expect the bot log line")
    print('  ChatManager: Slack adapter "..." using socket mode')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-token",
        default=os.environ.get("SLACK_APP_TOKEN", ""),
        help="Slack app-level token (xapp-...). Defaults to $SLACK_APP_TOKEN.",
    )
    parser.add_argument(
        "--connection-id",
        default=None,
        help="Target a specific Slack connection id (optional).",
    )
    args = parser.parse_args()

    if not args.app_token or not args.app_token.startswith("xapp-"):
        print(
            "ERROR: provide a valid app-level token via --app-token or "
            "SLACK_APP_TOKEN (must start with 'xapp-').",
            file=sys.stderr,
        )
        return 1

    return asyncio.run(_run(args.connection_id, args.app_token))


if __name__ == "__main__":
    raise SystemExit(main())
