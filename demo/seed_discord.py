"""Post the demo conversation fixtures into a real Discord server.

The Discord twin of ``demo/seed_slack.py``. It reuses the SAME fixtures
(``demo/slack_demo_conversations.json``) so #basketball and #research get the
same multi-person student conversation.

Why webhooks (not the bot directly): a Discord BOT always posts under its own
name, so it can't render an 8-person conversation. A Discord WEBHOOK can override
``username`` + ``avatar_url`` per message (the same trick Slack's
``chat.postMessage`` username-override gives us). So the bot token is used only to
(1) discover the server's text channels and (2) create/reuse one webhook per
channel; the messages themselves are sent through the webhook. Webhook messages
are normal channel messages, so Beever ingests them on sync just like any other.

Prerequisites
-------------
1. A Discord application + bot (https://discord.com/developers/applications):
   - Bot tab: copy the BOT TOKEN; enable Message Content Intent + Server
     Members Intent (privileged intents).
   - General Information: note the Application ID + Public Key (needed later for
     the Beever connection, NOT for this script).
2. The bot invited to your server (OAuth2 URL generator → scope ``bot``) with:
   Manage Webhooks, Send Messages, Read Message History
   (+ Manage Messages if you want ``--purge`` to clean prior runs).
3. Text channels named to match the fixtures: ``basketball`` and ``research``.

Usage
-----
    export DISCORD_BOT_TOKEN=...                 # Bot tab token

    # Offline preview (no token needed):
    python demo/seed_discord.py --fixtures demo/slack_demo_conversations.json --dry-run

    # Post for real (auto-detects the server if the bot is in exactly one):
    python demo/seed_discord.py --fixtures demo/slack_demo_conversations.json

    # Re-seed cleanly (delete prior bot/webhook messages first; needs Manage Messages):
    python demo/seed_discord.py --fixtures demo/slack_demo_conversations.json --purge

Notes
-----
- Per-author avatars are generated from DiceBear (stable per name, no setup).
- Slack thread replies are posted flat, in chronological order — Discord webhook
  messages can't carry a reply-reference, so the conversation reads top-to-bottom
  (still grouped sensibly because the fixtures order replies after their parent).
- Discord auto-embeds bare URLs, so the link cards + the basketball image render.
- Reactions are skipped (webhooks can't react).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo.seed_discord")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_FIXTURES = _PROJECT_ROOT / "tests" / "fixtures" / "slack_conversations.json"

_API = "https://discord.com/api/v10"
_WEBHOOK_NAME = "Beever Demo Seeder"
_POST_DELAY_SECONDS = 1.1  # webhook execute is ~5/2s per webhook; stay safe


def _avatar_url(name: str) -> str:
    """Stable, setup-free per-author avatar (DiceBear)."""
    return f"https://api.dicebear.com/9.x/avataaars/png?seed={quote(name)}"


def _load_fixtures(path: Path) -> dict[str, Any]:
    if not path.exists():
        logger.error("Fixtures not found at %s", path)
        sys.exit(1)
    return json.loads(path.read_text())


def _bot_request(method: str, path: str, token: str, **kw: Any) -> requests.Response:
    """Call the Discord REST API with bot auth, retrying on 429 rate limits."""
    url = f"{_API}{path}"
    headers = {"Authorization": f"Bot {token}", "Content-Type": "application/json"}
    for _ in range(6):
        resp = requests.request(method, url, headers=headers, timeout=30, **kw)
        if resp.status_code == 429:
            retry = float(resp.json().get("retry_after", 1.0))
            logger.warning("rate limited on %s %s — sleeping %.1fs", method, path, retry)
            time.sleep(retry + 0.2)
            continue
        return resp
    return resp


def _resolve_guild(token: str, guild_id: str | None) -> str:
    if guild_id:
        return guild_id
    resp = _bot_request("GET", "/users/@me/guilds", token)
    if resp.status_code != 200:
        logger.error("Could not list the bot's servers (%d): %s", resp.status_code, resp.text[:200])
        sys.exit(1)
    guilds = resp.json()
    if len(guilds) == 1:
        logger.info("Auto-detected server: %s (%s)", guilds[0]["name"], guilds[0]["id"])
        return guilds[0]["id"]
    if not guilds:
        logger.error("The bot isn't in any server. Invite it first, then re-run.")
        sys.exit(1)
    names = ", ".join(f"{g['name']}={g['id']}" for g in guilds)
    logger.error("Bot is in multiple servers — pass --guild-id. Options: %s", names)
    sys.exit(1)


def _list_text_channels(token: str, guild_id: str) -> dict[str, str]:
    """Return {channel_name: channel_id} for GUILD_TEXT channels (type 0)."""
    resp = _bot_request("GET", f"/guilds/{guild_id}/channels", token)
    if resp.status_code != 200:
        logger.error("Could not list channels (%d): %s", resp.status_code, resp.text[:200])
        sys.exit(1)
    return {c["name"]: c["id"] for c in resp.json() if c.get("type") == 0}


def _get_or_create_webhook(token: str, channel_id: str) -> str:
    """Return a webhook URL for the channel, reusing ours if it exists."""
    resp = _bot_request("GET", f"/channels/{channel_id}/webhooks", token)
    if resp.status_code == 200:
        for wh in resp.json():
            if wh.get("name") == _WEBHOOK_NAME and wh.get("token"):
                return f"{_API}/webhooks/{wh['id']}/{wh['token']}"
    elif resp.status_code == 403:
        logger.error(
            "Bot lacks Manage Webhooks in channel %s — grant it and re-invite.", channel_id
        )
        sys.exit(1)
    created = _bot_request(
        "POST", f"/channels/{channel_id}/webhooks", token, json={"name": _WEBHOOK_NAME}
    )
    if created.status_code not in (200, 201):
        logger.error(
            "Webhook create failed for %s (%d): %s",
            channel_id,
            created.status_code,
            created.text[:200],
        )
        sys.exit(1)
    wh = created.json()
    return f"{_API}/webhooks/{wh['id']}/{wh['token']}"


def _purge_channel(token: str, channel_id: str) -> int:
    """Best-effort delete of recent bot/webhook messages (needs Manage Messages)."""
    deleted = 0
    resp = _bot_request("GET", f"/channels/{channel_id}/messages?limit=100", token)
    while resp.status_code == 200 and resp.json():
        ids = [
            m["id"] for m in resp.json() if m.get("webhook_id") or m.get("author", {}).get("bot")
        ]
        if not ids:
            break
        if len(ids) >= 2:
            r = _bot_request(
                "POST",
                f"/channels/{channel_id}/messages/bulk-delete",
                token,
                json={"messages": ids},
            )
            if r.status_code in (200, 204):
                deleted += len(ids)
            elif r.status_code == 403:
                logger.warning("No Manage Messages perm — skipping purge for %s", channel_id)
                return deleted
            else:
                break
        else:
            r = _bot_request("DELETE", f"/channels/{channel_id}/messages/{ids[0]}", token)
            if r.status_code in (200, 204):
                deleted += 1
            time.sleep(0.4)
        resp = _bot_request("GET", f"/channels/{channel_id}/messages?limit=100", token)
    return deleted


def _post(webhook_url: str, *, content: str, username: str) -> None:
    payload = {
        "content": content,
        "username": username[:80],
        "avatar_url": _avatar_url(username),
        "allowed_mentions": {"parse": []},  # never ping anyone from seeded text
    }
    for _ in range(6):
        resp = requests.post(webhook_url, json=payload, timeout=30)
        if resp.status_code == 429:
            retry = float(resp.json().get("retry_after", 1.0))
            time.sleep(retry + 0.2)
            continue
        if resp.status_code not in (200, 204):
            logger.error("webhook post failed (%d): %s", resp.status_code, resp.text[:200])
        return


def _preview(
    data: dict[str, Any], users: dict[str, Any], fixture_channels: list[dict[str, Any]]
) -> None:
    for ch in fixture_channels:
        messages = data.get("messages", {}).get(ch["channel_id"], [])
        logger.info("=== #%s : %d messages ===", ch["name"], len(messages))
        for m in messages:
            author = users.get(m.get("author", ""), {}).get("name", m.get("author"))
            text = m.get("content", "").replace("\n", " ")
            logger.info("  %s: %s", author, text[:90])
    logger.info("Dry run complete (offline). No messages were posted.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("DISCORD_BOT_TOKEN", ""),
        help="Bot token. Defaults to $DISCORD_BOT_TOKEN.",
    )
    parser.add_argument("--fixtures", default=str(_FIXTURES), help="Conversation fixtures JSON.")
    parser.add_argument(
        "--guild-id",
        default=None,
        help="Server id (only needed if the bot is in more than one server).",
    )
    parser.add_argument(
        "--channel-map", default=None, help='Override name->id, e.g. "basketball=123,research=456".'
    )
    parser.add_argument(
        "--purge",
        action="store_true",
        help="Delete prior bot/webhook messages first (needs Manage Messages).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Offline preview; post nothing.")
    parser.add_argument(
        "--only", default=None, help="Comma-separated fixture channel names to seed."
    )
    args = parser.parse_args()

    data = _load_fixtures(Path(args.fixtures))
    users = data.get("users", {})
    fixture_channels = [
        c for c in data.get("channels", []) if c.get("platform", "slack") in ("slack", "discord")
    ]
    only = {n.strip().lstrip("#") for n in args.only.split(",")} if args.only else None
    if only:
        fixture_channels = [c for c in fixture_channels if c["name"] in only]
    if not fixture_channels:
        logger.error("No matching channels in fixtures.")
        sys.exit(1)

    if args.dry_run:
        _preview(data, users, fixture_channels)
        return

    if not args.token:
        logger.error("No bot token. Set DISCORD_BOT_TOKEN or pass --token.")
        sys.exit(1)

    explicit_map: dict[str, str] = {}
    if args.channel_map:
        for pair in args.channel_map.split(","):
            name, _, cid = pair.strip().partition("=")
            explicit_map[name.strip().lstrip("#")] = cid.strip()

    guild_id = _resolve_guild(args.token, args.guild_id)
    discovered = explicit_map or _list_text_channels(args.token, guild_id)

    # Resolve fixture name -> channel id.
    target: dict[str, str] = {}
    for ch in fixture_channels:
        cid = explicit_map.get(ch["name"]) or discovered.get(ch["name"])
        if not cid:
            logger.error(
                "Could not find a Discord text channel named '#%s' — create it (or use --channel-map).",
                ch["name"],
            )
            sys.exit(1)
        target[ch["name"]] = cid
        logger.info("Channel #%s -> %s", ch["name"], cid)

    if args.purge:
        for ch in fixture_channels:
            n = _purge_channel(args.token, target[ch["name"]])
            logger.info("Purged %d prior message(s) from #%s", n, ch["name"])

    total = 0
    for ch in fixture_channels:
        webhook_url = _get_or_create_webhook(args.token, target[ch["name"]])
        messages = data.get("messages", {}).get(ch["channel_id"], [])
        logger.info("=== #%s : %d messages ===", ch["name"], len(messages))
        for m in messages:
            author = users.get(m.get("author", ""), {}).get("name", m.get("author") or "Someone")
            content = m.get("content", "")
            image_url = m.get("image_url")
            if image_url:  # Discord auto-embeds a bare URL on its own line
                content = f"{content}\n{image_url}" if content else image_url
            _post(webhook_url, content=content, username=author)
            total += 1
            time.sleep(_POST_DELAY_SECONDS)

    logger.info("Done. Posted %d messages across %d channel(s).", total, len(fixture_channels))
    logger.info("Now connect this server in Beever (Settings -> Connections -> Discord) and sync.")


if __name__ == "__main__":
    main()
