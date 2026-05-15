"""One-shot migration: legacy ``wiki_cache.pages.{page_id}`` → ``wiki_pages``.

The pre-redesign LLM Wiki stored every page for a channel as a subdoc on
ONE ``wiki_cache`` document keyed by ``channel_id`` (or
``channel_id:<target_lang>`` for non-default langs). The redesign moved to
a per-page collection (``wiki_pages``) so the WikiMaintainer can update
ONE page without rewriting all of them.

This script reads each legacy ``wiki_cache`` row, splits its ``pages``
dict into per-page rows, and upserts them into ``wiki_pages`` with
``is_dirty=True`` so the next maintainer run rewrites them through the
new prompt path. Existing per-page rows with ``version > 1`` (i.e. ones
the new path has already been writing) are NEVER overwritten.

Idempotency
-----------
``wiki_pages`` carries a compound unique index on
``(channel_id, target_lang, page_id)``. Re-running the script after a
successful run is a no-op for inserted rows; existing rows are either
skipped (version > 1) or refreshed via ``$set`` while ``$setOnInsert``
preserves their ``created_at``.

Resumability
------------
Tracks ``last_processed_id`` (the legacy ``wiki_cache._id``) in the
``migration_state`` collection under key ``wiki_cache_to_pages``. On
restart, resumes from ``{"_id": {"$gt": <last_processed_id>}}`` so a
Ctrl+C halfway through is safe.

Usage
-----
::

    # Dry run: count what would be migrated, log a sample, no writes.
    uv run python -m beever_atlas.scripts.migrate_wiki_cache_to_pages --dry-run

    # Migrate everything in 100-row batches.
    uv run python -m beever_atlas.scripts.migrate_wiki_cache_to_pages

    # Pilot a single channel before flipping ``PER_PAGE_WIKI=ON``.
    uv run python -m beever_atlas.scripts.migrate_wiki_cache_to_pages --channel-id abc-123

    # Tune batch size for huge collections.
    uv run python -m beever_atlas.scripts.migrate_wiki_cache_to_pages --batch-size 50

The script logs structured JSON progress every batch and a final
``migration_complete`` line on success.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Load .env so MONGODB_URI is honoured when invoked via `uv run`.
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

from beever_atlas.infra.config import get_settings  # noqa: E402
from beever_atlas.models.persistence import (  # noqa: E402
    WikiPage,
    WikiPageSection,
)


logger = logging.getLogger("beever_atlas.scripts.migrate_wiki_cache_to_pages")


_STATE_KEY = "wiki_cache_to_pages"


def _setup_logging() -> None:
    """Structured-JSON logging so progress lines are easy to parse."""

    class JsonFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            payload: dict[str, Any] = {
                "ts": datetime.now(tz=UTC).isoformat(),
                "level": record.levelname.lower(),
                "msg": record.getMessage(),
                "logger": record.name,
            }
            return json.dumps(payload, ensure_ascii=False)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    logger.setLevel(logging.INFO)


def _split_legacy_key(key: str, default_lang: str) -> tuple[str, str]:
    """Recover ``(channel_id, target_lang)`` from a legacy cache key.

    The legacy code keyed default-language docs by bare ``channel_id``
    and non-default by ``channel_id:<lang>``. We treat any non-empty
    trailing segment after a colon as a lang code, IF the segment looks
    like a 2-3 letter ISO code (so a colon inside a Slack channel id
    like ``C123:DM`` is left alone).
    """
    if ":" in key:
        prefix, suffix = key.rsplit(":", 1)
        if 2 <= len(suffix) <= 5 and suffix.isalpha():
            return prefix, suffix
    return key, default_lang


def _legacy_subdoc_to_page(
    *,
    channel_id: str,
    target_lang: str,
    page_id: str,
    subdoc: dict[str, Any],
) -> WikiPage | None:
    """Map one legacy ``pages[page_id]`` subdoc into a ``WikiPage`` row.

    Tolerates legacy schema variations: missing fields fall back to safe
    defaults; ``content`` (markdown blob) becomes a single ``"overview"``
    section; ``sections`` (list-of-dict) becomes a 1:1 ``WikiPageSection``
    list. Returns None if the subdoc is too malformed to map.
    """
    if not isinstance(subdoc, dict) or not page_id:
        return None

    title = str(subdoc.get("title") or page_id)
    slug = str(subdoc.get("slug") or page_id.replace(":", "-"))

    sections: list[WikiPageSection] = []
    raw_sections = subdoc.get("sections")
    if isinstance(raw_sections, list):
        for idx, sec in enumerate(raw_sections):
            if not isinstance(sec, dict):
                continue
            sec_id = str(sec.get("id") or f"section-{idx}")
            sec_title = str(sec.get("title") or sec_id.replace("-", " ").title())
            sec_content = str(sec.get("content_md") or sec.get("content") or "")
            sections.append(
                WikiPageSection(
                    id=sec_id,
                    title=sec_title,
                    content_md=sec_content,
                )
            )
    elif isinstance(subdoc.get("content"), str):
        sections.append(
            WikiPageSection(
                id="overview",
                title="Overview",
                content_md=str(subdoc["content"]),
            )
        )

    if not sections:
        # No content at all → still create a stub page (the maintainer
        # will rewrite it on next run) so the per-page UI doesn't 404.
        sections = [WikiPageSection(id="overview", title="Overview", content_md="")]

    return WikiPage(
        channel_id=channel_id,
        target_lang=target_lang,
        page_id=page_id,
        title=title,
        slug=slug,
        sections=sections,
        is_dirty=True,
    )


async def _migrate(args: argparse.Namespace) -> None:
    from beever_atlas.stores import StoreClients

    settings = get_settings()
    stores = StoreClients.from_settings(settings)
    await stores.startup()

    db = stores.mongodb.db
    cache_coll = db["wiki_cache"]
    pages_coll = db["wiki_pages"]
    state_coll = db["migration_state"]
    default_lang = settings.default_target_language or "en"

    # ``wiki_pages`` indexes are owned by ``WikiPageStore.ensure_indexes`` and
    # the FastAPI app lifespan calls them on startup. The CLI bypasses that
    # lifespan, so on a fresh database the compound unique index on
    # ``(channel_id, target_lang, page_id)`` would not exist and re-running
    # the migration after a partial failure could insert duplicate rows.
    # Ensure the indexes here before the upsert loop so idempotency holds.
    from beever_atlas.wiki.page_store import WikiPageStore

    page_store = WikiPageStore(db=db)
    await page_store.ensure_indexes()

    state = await state_coll.find_one({"_id": _STATE_KEY})
    last_processed_id = state.get("last_processed_id") if state else None

    query: dict[str, Any] = {}
    if last_processed_id is not None:
        query["_id"] = {"$gt": last_processed_id}
    if args.channel_id:
        # The legacy schema stored ``channel_id`` as either the bare id or
        # ``id:lang`` — match both forms with a startsWith filter on the
        # target id (still cheap because the collection is small).
        query["channel_id"] = {"$regex": f"^{args.channel_id}(:.*)?$"}

    cursor = cache_coll.find(query, sort=[("_id", 1)], batch_size=args.batch_size)

    counters = {
        "scanned_legacy_rows": 0,
        "would_migrate": 0,
        "migrated": 0,
        "skipped_active_page": 0,
        "skipped_malformed": 0,
        "errors": 0,
    }
    sample_limit = 5
    samples: list[dict[str, Any]] = []

    try:
        async for legacy_doc in cursor:
            counters["scanned_legacy_rows"] += 1
            legacy_key = str(legacy_doc.get("channel_id") or "")
            channel_id, target_lang = _split_legacy_key(legacy_key, default_lang)
            pages_dict = legacy_doc.get("pages") or {}
            if not isinstance(pages_dict, dict):
                counters["skipped_malformed"] += 1
                continue

            for page_id, subdoc in pages_dict.items():
                page = _legacy_subdoc_to_page(
                    channel_id=channel_id,
                    target_lang=target_lang,
                    page_id=str(page_id),
                    subdoc=subdoc,
                )
                if page is None:
                    counters["skipped_malformed"] += 1
                    continue

                # Skip if the new path has already been writing this page
                # (version > 1 → don't clobber active edits).
                existing = await pages_coll.find_one(
                    {
                        "channel_id": channel_id,
                        "target_lang": target_lang,
                        "page_id": str(page_id),
                    },
                    {"_id": 0, "version": 1},
                )
                if existing and int(existing.get("version", 0) or 0) > 1:
                    counters["skipped_active_page"] += 1
                    if logger.isEnabledFor(logging.INFO):
                        logger.info(
                            "skipped_active_page channel_id=%s page_id=%s existing_version=%s",
                            channel_id,
                            page_id,
                            existing.get("version"),
                        )
                    continue

                if args.dry_run:
                    counters["would_migrate"] += 1
                    if len(samples) < sample_limit:
                        samples.append(
                            {
                                "channel_id": channel_id,
                                "target_lang": target_lang,
                                "page_id": str(page_id),
                                "title": page.title,
                                "section_count": len(page.sections),
                            }
                        )
                    continue

                try:
                    doc = page.model_dump(mode="json")
                    doc.pop("version", None)
                    created_at = doc.pop("created_at", None) or datetime.now(tz=UTC).isoformat()
                    doc["updated_at"] = datetime.now(tz=UTC).isoformat()
                    update: dict[str, Any] = {
                        "$set": doc,
                        "$inc": {"version": 1},
                        "$setOnInsert": {"created_at": created_at},
                    }
                    await pages_coll.update_one(
                        {
                            "channel_id": channel_id,
                            "target_lang": target_lang,
                            "page_id": str(page_id),
                        },
                        update,
                        upsert=True,
                    )
                    counters["migrated"] += 1
                except Exception:  # noqa: BLE001 — keep going on per-page errors
                    counters["errors"] += 1
                    logger.exception(
                        "wiki_cache_to_pages: per-page upsert failed channel_id=%s page_id=%s",
                        channel_id,
                        page_id,
                    )

            # Update resume state after each legacy row so a Ctrl+C in the
            # middle of a row only re-processes that row's pages (which are
            # already idempotent via the compound key).
            if not args.dry_run and legacy_doc.get("_id") is not None:
                await state_coll.update_one(
                    {"_id": _STATE_KEY},
                    {
                        "$set": {
                            "last_processed_id": legacy_doc["_id"],
                            "updated_at": datetime.now(tz=UTC).isoformat(),
                        }
                    },
                    upsert=True,
                )

            if counters["scanned_legacy_rows"] % args.batch_size == 0:
                logger.info(
                    "progress " + json.dumps(counters, ensure_ascii=False),
                )
    finally:
        await stores.shutdown()

    if args.dry_run:
        logger.info(
            "migration_dry_run_complete "
            + json.dumps({**counters, "sample": samples}, ensure_ascii=False)
        )
    else:
        logger.info("migration_complete " + json.dumps(counters, ensure_ascii=False))


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="migrate_wiki_cache_to_pages",
        description="Migrate legacy wiki_cache.pages.* subdocs to per-page wiki_pages rows.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Count what would be migrated; log a sample; no writes.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Legacy-row fetch batch size (also progress-log cadence). Default: 100.",
    )
    parser.add_argument(
        "--channel-id",
        type=str,
        default=None,
        help="Restrict migration to one channel_id (safe pilot before flag flip).",
    )
    return parser


def main() -> None:
    _setup_logging()
    args = _build_arg_parser().parse_args()
    asyncio.run(_migrate(args))


if __name__ == "__main__":
    main()
