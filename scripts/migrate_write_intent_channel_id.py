"""Backfill ``write_intents.channel_id`` for delete-channel-v2 (Wave 1).

The hard-purge of a channel deletes its ``write_intents`` in one indexed
pass via the top-level ``channel_id`` field added in Wave 1
(``models/persistence.WriteIntent.channel_id``). Rows written before that
field existed have no top-level ``channel_id`` — this script derives it
from the nested ``facts[].channel_id`` (``models/domain.AtomicFact``):

  * If EVERY fact in the intent shares one non-empty ``channel_id``, that
    value is written top-level.
  * If the facts span more than one channel (mixed-channel intent), the
    top-level field is left ``None`` — the WriteReconciler's per-fact
    channel filter (Wave 0) neutralises those rows during a purge, so a
    coarse top-level value would be wrong.
  * If there are no facts, or none carries a ``channel_id``, the row is
    left untouched (``None``).

Idempotent + re-runnable: a row that already has a non-null top-level
``channel_id`` is skipped, and a row whose facts are mixed/empty is left
``None`` on every pass (it never becomes "orphaned").

Usage:
    MONGODB_URI=mongodb://... \
      uv run python -m scripts.migrate_write_intent_channel_id \
      [--dry-run] [--channel-id <id>]

The MongoDB URI is read from the ``MONGODB_URI`` environment variable (NOT a
CLI flag) so the credential-bearing connection string never lands in ``ps``
output. Defaults to ``mongodb://localhost:27017/beever_atlas``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)


def _derive_channel_id(facts: list[dict[str, Any]] | None) -> str | None:
    """Return the single channel_id shared by ``facts``, else ``None``.

    ``None`` is returned for empty / channel-less facts AND for
    mixed-channel intents — both are deliberately left unbackfilled so the
    reconciler's per-fact filter stays the authority for those rows.
    """
    if not facts:
        return None
    channel_ids = {
        cid for fact in facts if isinstance(fact, dict) and (cid := fact.get("channel_id"))
    }
    if len(channel_ids) == 1:
        return next(iter(channel_ids))
    # Zero channel_ids (none carry one) or more than one (mixed) → leave None.
    return None


async def migrate(
    *,
    mongodb_uri: str,
    channel_id: str | None,
    dry_run: bool,
    db_name: str = "beever_atlas",
) -> dict[str, int]:
    """Run the backfill. Returns counters: planned / written / skipped / mixed.

    ``skipped`` counts rows that already have a non-null top-level
    ``channel_id`` (idempotent re-run). ``mixed`` counts rows left ``None``
    because their facts span >1 channel or carry none. On ``dry_run=True``
    every "would-write" lands in ``planned`` and ``written`` stays 0.

    The optional ``channel_id`` filter restricts the backfill to rows whose
    DERIVED channel matches — it scans every row missing a top-level value,
    derives the channel, and only writes when the derivation equals the
    requested channel. This keeps a targeted backfill safe without assuming
    the field it is trying to populate already exists.
    """
    client = AsyncIOMotorClient(mongodb_uri)
    try:
        db = client[db_name]
        coll = db["write_intents"]

        counters = {"planned": 0, "written": 0, "skipped": 0, "mixed": 0}

        # Only inspect rows that lack a usable top-level channel_id. A row
        # whose field is already a non-null string is fully migrated.
        query: dict[str, Any] = {
            "$or": [
                {"channel_id": {"$exists": False}},
                {"channel_id": None},
            ]
        }

        async for doc in coll.find(query):
            # Defensive: a row could match the query yet already hold a
            # value if a concurrent writer set it between scan + read.
            existing = doc.get("channel_id")
            if existing:
                counters["skipped"] += 1
                continue

            derived = _derive_channel_id(doc.get("facts"))
            if derived is None:
                # Mixed-channel or channel-less — intentionally left None.
                counters["mixed"] += 1
                continue
            if channel_id is not None and derived != channel_id:
                # Targeted backfill: row belongs to a different channel.
                counters["skipped"] += 1
                continue

            # Defensive: the update below filters on ``id``. A legacy row missing
            # that field would make the filter ``{"id": None}`` match nothing —
            # silently losing the write while still counting it as written. Skip
            # such rows explicitly. (App-created rows always have a UUID ``id``.)
            if not doc.get("id"):
                counters["skipped"] += 1
                logger.warning("write_intent missing 'id' field, skipping: _id=%s", doc.get("_id"))
                continue

            counters["planned"] += 1
            if dry_run:
                logger.info(
                    "would-write intent=%s channel_id=%s",
                    doc.get("id"),
                    derived,
                )
                continue

            await coll.update_one(
                {"id": doc.get("id")},
                {"$set": {"channel_id": derived}},
            )
            counters["written"] += 1

        return counters
    finally:
        client.close()


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan + log changes but write nothing.",
    )
    parser.add_argument(
        "--channel-id",
        default=None,
        help="Restrict the backfill to rows whose derived channel matches.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    # Read the URI from the environment — NOT argv. A connection string carries
    # credentials, and argv is visible to any local user via ``ps``.
    mongodb_uri = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/beever_atlas")
    counters = await migrate(
        mongodb_uri=mongodb_uri,
        channel_id=args.channel_id,
        dry_run=args.dry_run,
    )
    logger.info(
        "migrate_write_intent_channel_id finished planned=%d written=%d "
        "skipped=%d mixed=%d dry_run=%s",
        counters["planned"],
        counters["written"],
        counters["skipped"],
        counters["mixed"],
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
