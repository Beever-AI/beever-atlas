"""Hard-delete archived ``kind="entity"`` wiki pages after retention.

Companion to ``archive_kind_entity_pages.py``: removes rows where
``kind="entity" AND archived=true AND archived_at < now - retention``. The
operator runs this AFTER the retention window has elapsed (default 30 days).

Usage:

    python -m beever_atlas.scripts.drop_archived_kind_entity_pages \
        [--dry-run] [--channel-id <id>] [--batch-size 500] \
        [--min-archived-age-days 30] [--confirm]

Without ``--confirm`` the script refuses to delete (safety net for accidental
invocations). Drop is irreversible — there is no undo flag.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger("drop_archived_kind_entity_pages")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be deleted without writing.",
    )
    p.add_argument(
        "--channel-id",
        type=str,
        default=None,
        help="Limit the drop to one channel (safe pilot mode).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of rows to delete per Mongo delete_many call.",
    )
    p.add_argument(
        "--min-archived-age-days",
        type=int,
        default=30,
        help="Only drop rows whose archived_at is older than this many days.",
    )
    p.add_argument(
        "--confirm",
        action="store_true",
        help="Required for non-dry-run mode. Drop is IRREVERSIBLE.",
    )
    p.add_argument(
        "--target-lang",
        type=str,
        default=None,
        help="Optional target_lang filter (default: all languages).",
    )
    return p.parse_args(argv)


async def drop_archived_kind_entity_pages(
    *,
    channel_id: str | None,
    batch_size: int,
    dry_run: bool,
    min_archived_age_days: int,
    target_lang: str | None,
) -> dict[str, int]:
    """Drive the delete loop and return a stats dict."""
    from beever_atlas.stores import get_stores

    stores = get_stores()
    db = stores.mongodb._db  # noqa: SLF001 — script directly accesses driver
    coll = db["wiki_pages"]

    cutoff = (datetime.now(tz=UTC) - timedelta(days=min_archived_age_days)).isoformat()
    base_query: dict[str, Any] = {
        "kind": "entity",
        "archived": True,
        "archived_at": {"$lt": cutoff},
    }
    if channel_id:
        base_query["channel_id"] = channel_id
    if target_lang:
        base_query["target_lang"] = target_lang

    pre_count = await coll.count_documents(base_query)
    logger.info(
        "drop_archived_kind_entity_pages: pre_count=%d channel_id=%s "
        "target_lang=%s min_age_days=%d cutoff=%s dry_run=%s",
        pre_count,
        channel_id or "*",
        target_lang or "*",
        min_archived_age_days,
        cutoff,
        dry_run,
    )
    if not pre_count or dry_run:
        return {"matched": pre_count, "deleted": 0, "batches": 0 if dry_run else 0}

    total_deleted = 0
    batches = 0
    while True:
        # Fetch a page of _ids to delete in this batch (Mongo's delete_many
        # without limit would walk the whole result set in one shot, which
        # is fine for ~1k rows but unkind for ~1M).
        cursor = coll.find(base_query).limit(batch_size)
        targets: list[Any] = []
        async for doc in cursor:
            targets.append(doc.get("_id"))
        if not targets:
            break
        result = await coll.delete_many({"_id": {"$in": targets}})
        deleted = int(getattr(result, "deleted_count", 0))
        if deleted == 0:
            break
        total_deleted += deleted
        batches += 1

    return {"matched": pre_count, "deleted": total_deleted, "batches": batches}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if not args.dry_run and not args.confirm:
        logger.error(
            "Refusing to drop without --confirm flag. Drop is IRREVERSIBLE. "
            "Re-run with --dry-run first to inspect, then --confirm to delete."
        )
        return 2
    stats = asyncio.run(
        drop_archived_kind_entity_pages(
            channel_id=args.channel_id,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            min_archived_age_days=args.min_archived_age_days,
            target_lang=args.target_lang,
        )
    )
    logger.info(
        "drop_archived_kind_entity_pages: %s matched=%d deleted=%d batches=%d",
        "dry-run" if args.dry_run else "complete",
        stats["matched"],
        stats["deleted"],
        stats["batches"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
