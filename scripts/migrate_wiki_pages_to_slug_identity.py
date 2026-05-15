"""Backfill wiki_pages.slug + kind for the wiki-llm-native-redesign (§8.5).

Walks every wiki page that lacks a non-empty slug or has the model-default
``kind="topic"`` despite a structurally non-topic ``page_id``, derives the
canonical slug + kind, and writes them in place. Idempotent — a re-run
makes no changes for already-migrated rows.

Slug derivation rules (matching the maintainer's first-touch behaviour):
  * Use the page's title when non-empty (kebab-case ASCII fallback).
  * Otherwise use ``page_id.replace(":", "-")``.
  * Dedupe collisions per (channel_id, target_lang) by appending ``-2``,
    ``-3``, ... in stable order (oldest-created wins; ties broken by
    page_id).

Kind derivation matches ``services.wiki_maintainer._derive_kind_from_page_id``:
  * ``topic:*`` → ``topic`` (default; only writes if currently ``""``).
  * ``entity:*`` → ``entity``.
  * ``decisions`` → ``decisions``.
  * ``faq`` → ``faq``.
  * ``action-items`` → ``action_items``.

Usage:
    uv run python -m scripts.migrate_wiki_pages_to_slug_identity \
      [--dry-run] [--channel-id <id>]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from beever_atlas.wiki.slugify import slugify as _slugify_shared

logger = logging.getLogger(__name__)


def _slugify(title: str, page_id: str) -> str:
    """Thin wrapper over ``beever_atlas.wiki.slugify.slugify``.

    The shared utility owns the canonical derivation so the offline
    migration and the curation HTTP API never produce different slugs
    for the same title (code-review finding §M).
    """
    return _slugify_shared(title, fallback_page_id=page_id)


def _derive_kind(page_id: str) -> str:
    """Mirror ``wiki_maintainer._derive_kind_from_page_id`` so the
    migration produces values the live dispatcher will respect."""
    if not page_id:
        return "topic"
    if page_id.startswith("entity:"):
        return "entity"
    if page_id == "decisions":
        return "decisions"
    if page_id == "faq":
        return "faq"
    if page_id == "action-items":
        return "action_items"
    return "topic"


async def migrate(
    *,
    mongodb_uri: str,
    channel_id: str | None,
    dry_run: bool,
    db_name: str = "beever_atlas",
) -> dict[str, int]:
    """Run the migration. Returns counters: planned / written / skipped.

    On ``dry_run=True``, the function inspects every row but does NOT
    write — every "would-write" lands in ``planned`` and ``written``
    stays 0. The split lets ops verify the change set before committing.
    """
    client = AsyncIOMotorClient(mongodb_uri)
    try:
        db = client[db_name]
        coll = db["wiki_pages"]
        query: dict[str, Any] = {}
        if channel_id:
            query["channel_id"] = channel_id

        counters = {"planned": 0, "written": 0, "skipped": 0}
        # Group rows by (channel_id, target_lang) so the slug-collision
        # dedupe stays scoped — two channels can both have an
        # ``Authentication`` page; the slug must collide only within
        # one bucket.
        rows: list[dict[str, Any]] = []
        async for doc in coll.find(query):
            rows.append(doc)

        # Sort by created_at (oldest first; ties broken by page_id) so
        # the first occurrence "wins" the un-suffixed slug.
        rows.sort(key=lambda d: (d.get("created_at") or datetime.min, d.get("page_id") or ""))

        # Track slugs we've already assigned this run, per bucket.
        seen: dict[tuple[str, str], set[str]] = {}

        for doc in rows:
            cid = doc.get("channel_id") or ""
            lang = doc.get("target_lang") or "en"
            page_id = doc.get("page_id") or ""

            existing_slug = (doc.get("slug") or "").strip()
            existing_kind = (doc.get("kind") or "").strip()
            target_kind = _derive_kind(page_id)

            updates: dict[str, Any] = {}

            # Slug — only assign when missing.
            if not existing_slug:
                base = _slugify(doc.get("title") or "", page_id)
                bucket = seen.setdefault((cid, lang), set())
                # Pull existing slugs in this bucket from the DB so we
                # don't collide with rows we won't otherwise touch.
                if not bucket:
                    async for row in coll.find(
                        {
                            "channel_id": cid,
                            "target_lang": lang,
                            "slug": {"$gt": ""},
                        },
                        {"slug": 1},
                    ):
                        slug = row.get("slug")
                        if isinstance(slug, str) and slug:
                            bucket.add(slug)
                slug = base
                suffix = 2
                while slug in bucket:
                    slug = f"{base}-{suffix}"
                    suffix += 1
                bucket.add(slug)
                updates["slug"] = slug

            # Kind — only overwrite the model default when structural
            # derivation says otherwise.
            if (not existing_kind or existing_kind == "topic") and target_kind != "topic":
                updates["kind"] = target_kind

            if not updates:
                counters["skipped"] += 1
                continue

            counters["planned"] += 1
            if dry_run:
                logger.info(
                    "would-write channel=%s lang=%s page_id=%s updates=%s",
                    cid,
                    lang,
                    page_id,
                    updates,
                )
                continue

            updates["updated_at"] = datetime.now(tz=UTC).isoformat()
            await coll.update_one(
                {"channel_id": cid, "target_lang": lang, "page_id": page_id},
                {"$set": updates},
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
        help="Restrict the migration to a single channel.",
    )
    parser.add_argument(
        "--mongodb-uri",
        default="mongodb://localhost:27017/beever_atlas",
        help="MongoDB connection URI.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    counters = await migrate(
        mongodb_uri=args.mongodb_uri,
        channel_id=args.channel_id,
        dry_run=args.dry_run,
    )
    logger.info(
        "migrate_wiki_pages_to_slug_identity finished planned=%d written=%d skipped=%d dry_run=%s",
        counters["planned"],
        counters["written"],
        counters["skipped"],
        args.dry_run,
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
