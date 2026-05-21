"""Make webhook-seeded Discord demo messages ingestable, then re-extract them.

Why this exists
---------------
``demo/seed_discord.py`` posts the demo conversation through Discord *webhooks*
so each message can carry a per-author name + avatar (a Discord bot can't
rename itself per message). The catch: every webhook message has
``author.bot = true``, so the chat bridge stores ``raw_metadata.is_bot = true``.

Beever's :func:`PreprocessorAgent._is_skippable` deliberately drops bot
messages (CI/deploy/integration noise) *unless* they're a substantive thread
reply. Discord webhooks post flat (no reply reference), so EVERY seeded message
is dropped — extraction yields 0 facts and no wiki is built.

These demo messages represent real people, not integration bots — the
``is_bot`` flag is purely an artifact of the webhook trick. This script
corrects that on the already-ingested rows:

  * ``is_bot`` / ``raw_metadata.is_bot`` -> ``false``  (so the preprocessor keeps them)
  * ``extraction_status`` -> ``"pending"``, ``attempt_count`` -> 0,
    ``next_attempt_at`` -> now, ``last_error`` -> null
    (so the ExtractionWorker re-claims and re-extracts them)
  * any stuck ``worker_extraction`` sync_job for the channel -> ``"completed"``

It is idempotent and non-destructive (it flips flags + resets status; it does
not delete message content). The ExtractionWorker re-extracts on its next tick;
that runs consolidation, which lets the wiki generate.

Usage
-----
    # All webhook-seeded Discord channels (default):
    .venv/bin/python demo/fix_discord_ingest.py

    # Specific channel id(s):
    .venv/bin/python demo/fix_discord_ingest.py --channel-id <discord-channel-id>

The MongoDB URI is read from ``MONGODB_URI`` (default
``mongodb://localhost:27017/beever_atlas`` — the docker stack exposes 27017 on
localhost). It is NOT a CLI flag, so the connection string never lands in ``ps``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from datetime import datetime, timezone

from motor.motor_asyncio import AsyncIOMotorClient

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger("demo.fix_discord_ingest")


async def fix(
    *, mongodb_uri: str, channel_ids: list[str] | None, db_name: str = "beever_atlas"
) -> None:
    client = AsyncIOMotorClient(mongodb_uri)
    try:
        db = client[db_name]
        msgs = db["channel_messages"]

        # Default: every webhook-seeded Discord message. Optional: just the
        # channel(s) the caller named.
        base: dict = {"source_id": "discord"}
        if channel_ids:
            base["channel_id"] = {"$in": channel_ids}

        # Which channels are we touching? (for logging + sync_job cleanup)
        targets: list[str] = await msgs.distinct("channel_id", base)
        if not targets:
            logger.warning("No Discord channel_messages matched %s — nothing to do.", base)
            return

        now = datetime.now(timezone.utc)
        result = await msgs.update_many(
            base,
            {
                "$set": {
                    "is_bot": False,
                    "raw_metadata.is_bot": False,
                    "extraction_status": "pending",
                    "attempt_count": 0,
                    "next_attempt_at": now,
                    "last_error": None,
                }
            },
        )
        logger.info(
            "channel_messages: matched=%d modified=%d across %d channel(s): %s",
            result.matched_count,
            result.modified_count,
            len(targets),
            ", ".join(targets),
        )

        # Clear any stuck worker_extraction job so the UI doesn't show a
        # perpetual "syncing" state. Harmless if there are none.
        jobs = db["sync_jobs"]
        job_res = await jobs.update_many(
            {"channel_id": {"$in": targets}, "kind": "worker_extraction", "status": "running"},
            {"$set": {"status": "completed"}},
        )
        if job_res.modified_count:
            logger.info(
                "sync_jobs: marked %d stuck worker_extraction job(s) completed",
                job_res.modified_count,
            )

        logger.info("Done. The ExtractionWorker will re-extract these on its next tick.")
        logger.info(
            "Watch: docker logs -f beever-atlas-beever-atlas-1 | grep -E 'PreprocessorAgent|total_facts|consolidation complete'"
        )
    finally:
        client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--channel-id",
        action="append",
        default=None,
        help="Limit to this Discord channel id (repeatable). Default: all webhook-seeded Discord channels.",
    )
    args = parser.parse_args()
    mongodb_uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/beever_atlas")
    asyncio.run(fix(mongodb_uri=mongodb_uri, channel_ids=args.channel_id))


if __name__ == "__main__":
    main()
