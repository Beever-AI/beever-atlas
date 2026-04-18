"""One-off cleanup: remove duplicate Teams platform connections.

The connection-creation wizard's duplicate guard only looked at `bot_token`
(Slack/Discord/Mattermost key), so submitting the Teams form twice created
two rows with the same `appId`. The bot registers both adapters against the
same Azure Bot messaging endpoint, so only one of them ever receives a given
webhook — the other sits with an empty conversation registry, which is what
surfaces in Atlas as "No channels found".

This script keeps the most recently-updated Teams connection per `appId`
and deletes the rest (MongoDB + best-effort unregister from the bot).

Usage:
    dotenv run -- uv run python scripts/dedupe_teams_connections.py
    # or if using another env loader, ensure MONGODB_URI and BRIDGE_URL are set.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict

import httpx
from motor.motor_asyncio import AsyncIOMotorClient

from beever_atlas.infra.crypto import decrypt_credentials
from beever_atlas.models.platform_connection import PlatformConnection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def _from_doc(doc: dict) -> PlatformConnection:
    doc = dict(doc)
    doc.pop("_id", None)
    return PlatformConnection(**doc)


def _decrypt(conn: PlatformConnection) -> dict[str, str]:
    return decrypt_credentials(
        conn.encrypted_credentials, conn.credential_iv, conn.credential_tag,
    )


async def main() -> None:
    mongo_uri = os.environ.get("MONGODB_URI")
    if not mongo_uri:
        raise SystemExit("MONGODB_URI is not set — run via dotenv or export it first.")
    bridge_url = os.environ.get("BRIDGE_URL", "http://localhost:3001")
    bridge_key = os.environ.get("BRIDGE_API_KEY", "")

    client = AsyncIOMotorClient(mongo_uri)
    col = client["beever_atlas"]["platform_connections"]

    # Load all Teams rows.
    teams: list[PlatformConnection] = []
    async for doc in col.find({"platform": "teams"}):
        try:
            teams.append(_from_doc(doc))
        except Exception as exc:
            log.warning("Skipping unparseable teams doc %s: %s", doc.get("id"), exc)

    if not teams:
        log.info("No Teams connections found — nothing to clean up.")
        return

    # Group by appId. Unparseable credentials fall into a "_unknown" bucket so
    # they still get reported but aren't silently merged with real groups.
    groups: dict[str, list[PlatformConnection]] = defaultdict(list)
    for conn in teams:
        try:
            creds = _decrypt(conn)
            app_id = (creds.get("app_id") or creds.get("appId") or "").lower()
        except Exception as exc:
            log.warning("Cannot decrypt credentials for %s: %s", conn.id, exc)
            app_id = ""
        if not app_id:
            app_id = f"_unknown_{conn.id}"
        groups[app_id].append(conn)

    to_delete: list[PlatformConnection] = []
    for app_id, conns in groups.items():
        log.info("App %s — %d connection(s):", app_id, len(conns))
        # Newest updated_at wins. Created_at is the tiebreaker.
        conns.sort(key=lambda c: (c.updated_at, c.created_at), reverse=True)
        keep = conns[0]
        log.info("  KEEP  %s  (updated_at=%s display=%r)", keep.id, keep.updated_at, keep.display_name)
        for dup in conns[1:]:
            log.info("  DROP  %s  (updated_at=%s display=%r)", dup.id, dup.updated_at, dup.display_name)
            to_delete.append(dup)

    if not to_delete:
        log.info("Nothing to delete — every appId has a single connection.")
        return

    # Unregister from the bot first so it stops handling webhooks immediately.
    headers: dict[str, str] = {}
    if bridge_key:
        headers["Authorization"] = f"Bearer {bridge_key}"
    async with httpx.AsyncClient(base_url=bridge_url, headers=headers, timeout=15.0) as http:
        for conn in to_delete:
            try:
                resp = await http.delete(f"/bridge/adapters/{conn.id}")
                if resp.status_code not in (200, 404):
                    log.warning(
                        "Bridge unregister returned %d for %s — continuing with DB delete.",
                        resp.status_code, conn.id,
                    )
                else:
                    log.info("Unregistered %s from bot (status=%d).", conn.id, resp.status_code)
            except httpx.HTTPError as exc:
                log.warning("Bridge unreachable when unregistering %s: %s", conn.id, exc)

    # Then delete from MongoDB.
    for conn in to_delete:
        result = await col.delete_one({"id": conn.id})
        if result.deleted_count == 1:
            log.info("Deleted %s from platform_connections.", conn.id)
        else:
            log.warning("Delete miss for %s (already gone?).", conn.id)

    log.info("Done. Kept %d, deleted %d.", len(teams) - len(to_delete), len(to_delete))


if __name__ == "__main__":
    asyncio.run(main())
