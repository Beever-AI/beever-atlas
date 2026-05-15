"""Idempotent archive of legacy ``kind="entity"`` wiki pages.

Post wiki-redesign-gap-fill the maintainer no longer writes ``kind="entity"``
rows — entity intent is absorbed into the canonical ``people`` and ``glossary``
pages. This script flips ``archived=true`` on every existing legacy row so
operator surfaces stop showing them. The archive is reversible (``--unarchive``)
and idempotent (re-running is a no-op).

Usage:

    python -m beever_atlas.scripts.archive_kind_entity_pages \
        [--dry-run] [--channel-id <id>] [--batch-size 500] [--unarchive]

The script resumes via the ``migration_state`` collection key
``archive_kind_entity_pages`` so an interrupted run picks up where it left off.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("archive_kind_entity_pages")

_MIGRATION_KEY = "archive_kind_entity_pages"


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be archived without writing.",
    )
    p.add_argument(
        "--channel-id",
        type=str,
        default=None,
        help="Limit archive to one channel (safe pilot mode).",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Number of rows to flip per Mongo bulk_write call.",
    )
    p.add_argument(
        "--unarchive",
        action="store_true",
        help="Reverse the flag flip (sets archived=false). Reversible.",
    )
    p.add_argument(
        "--target-lang",
        type=str,
        default=None,
        help="Optional target_lang filter (default: all languages).",
    )
    p.add_argument(
        "--no-resume",
        action="store_true",
        help="Ignore migration_state checkpoint and re-process from scratch.",
    )
    return p.parse_args(argv)


async def _read_checkpoint(db: Any) -> dict[str, Any]:
    """Return the ``migration_state`` doc for this script, or empty dict."""
    coll = db["migration_state"]
    doc = await coll.find_one({"_id": _MIGRATION_KEY})
    return dict(doc or {})


async def _write_checkpoint(db: Any, payload: dict[str, Any]) -> None:
    """Upsert the ``migration_state`` doc for this script."""
    coll = db["migration_state"]
    payload = {**payload, "updated_at": datetime.now(tz=UTC).isoformat()}
    await coll.update_one(
        {"_id": _MIGRATION_KEY},
        {"$set": payload},
        upsert=True,
    )


async def _archive_one_batch(
    coll: Any,
    *,
    query: dict[str, Any],
    batch_size: int,
    archived_value: bool,
    dry_run: bool,
) -> int:
    """Find up to ``batch_size`` matching rows and flip their archived flag.

    Returns the count of rows modified. The query itself is idempotent
    (it filters on ``archived != target_value``), so the caller does not
    need a cursor position — re-running with the same scope skips the
    rows that already have the right value.
    """
    cursor = coll.find(query).limit(batch_size)
    targets: list[str] = []
    async for doc in cursor:
        targets.append(str(doc.get("_id")))
    if not targets:
        return 0
    if dry_run:
        return len(targets)
    # Build a single update_many — _id IN targets — to keep the round trip small.
    from bson import ObjectId

    object_ids = []
    string_ids = []
    for t in targets:
        try:
            object_ids.append(ObjectId(t))
        except Exception:  # noqa: BLE001 — handle non-ObjectId _id types
            string_ids.append(t)
    update = {
        "$set": {
            "archived": archived_value,
            "archived_at": datetime.now(tz=UTC).isoformat() if archived_value else None,
        }
    }
    modified = 0
    if object_ids:
        result = await coll.update_many({"_id": {"$in": object_ids}}, update)
        modified += int(getattr(result, "modified_count", 0))
    if string_ids:
        result = await coll.update_many({"_id": {"$in": string_ids}}, update)
        modified += int(getattr(result, "modified_count", 0))
    return modified


async def archive_kind_entity_pages(
    *,
    channel_id: str | None,
    batch_size: int,
    dry_run: bool,
    unarchive: bool,
    target_lang: str | None,
    resume: bool,
) -> dict[str, int]:
    """Drive the archive loop and return a stats dict.

    The loop terminates when ``_archive_one_batch`` returns 0 rows. The
    pre-archive count is sampled before the loop so dry-run reporting is
    accurate even for an empty collection.
    """
    from beever_atlas.stores import get_stores

    stores = get_stores()
    db = stores.mongodb._db  # noqa: SLF001 — script directly accesses the driver
    coll = db["wiki_pages"]

    base_query: dict[str, Any] = {"kind": "entity"}
    if channel_id:
        base_query["channel_id"] = channel_id
    if target_lang:
        base_query["target_lang"] = target_lang
    archived_value = not unarchive
    # Filter to rows that need flipping. Idempotent: re-running with the
    # same flag is a no-op because the rows already have the right value.
    if archived_value:
        base_query["archived"] = {"$ne": True}
    else:
        base_query["archived"] = True

    pre_count = await coll.count_documents(base_query)
    logger.info(
        "archive_kind_entity_pages: pre_count=%d channel_id=%s target_lang=%s "
        "unarchive=%s dry_run=%s",
        pre_count,
        channel_id or "*",
        target_lang or "*",
        unarchive,
        dry_run,
    )
    if not pre_count:
        if not dry_run:
            await _write_checkpoint(
                db,
                {
                    "channel_id": channel_id,
                    "target_lang": target_lang,
                    "unarchive": unarchive,
                    "rows_flipped": 0,
                    "completed": True,
                },
            )
        return {"matched": 0, "modified": 0, "batches": 0}

    if resume and not dry_run:
        prior = await _read_checkpoint(db)
        if (
            prior.get("channel_id") == channel_id
            and prior.get("target_lang") == target_lang
            and prior.get("unarchive") == unarchive
            and prior.get("completed") is True
        ):
            logger.info(
                "archive_kind_entity_pages: prior run already completed for this "
                "scope — skipping. Pass --no-resume to re-run."
            )
            return {"matched": 0, "modified": 0, "batches": 0}

    total_modified = 0
    batches = 0
    while True:
        modified = await _archive_one_batch(
            coll,
            query=base_query,
            batch_size=batch_size,
            archived_value=archived_value,
            dry_run=dry_run,
        )
        if modified == 0:
            break
        total_modified += modified
        batches += 1
        if not dry_run:
            await _write_checkpoint(
                db,
                {
                    "channel_id": channel_id,
                    "target_lang": target_lang,
                    "unarchive": unarchive,
                    "rows_flipped": total_modified,
                    "batches": batches,
                    "completed": False,
                },
            )
        # In dry-run mode the rows still match the query, so the loop
        # would never terminate — stop after one batch with the count.
        if dry_run:
            return {"matched": pre_count, "modified": total_modified, "batches": 1}

    if not dry_run:
        await _write_checkpoint(
            db,
            {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "unarchive": unarchive,
                "rows_flipped": total_modified,
                "batches": batches,
                "completed": True,
            },
        )
    return {"matched": pre_count, "modified": total_modified, "batches": batches}


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    stats = asyncio.run(
        archive_kind_entity_pages(
            channel_id=args.channel_id,
            batch_size=args.batch_size,
            dry_run=args.dry_run,
            unarchive=args.unarchive,
            target_lang=args.target_lang,
            resume=not args.no_resume,
        )
    )
    logger.info(
        "archive_kind_entity_pages: %s matched=%d modified=%d batches=%d",
        "dry-run" if args.dry_run else "complete",
        stats["matched"],
        stats["modified"],
        stats["batches"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
