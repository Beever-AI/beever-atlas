"""MongoDB-backed cache for compiled wiki documents."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from beever_atlas.infra.config import get_settings
from beever_atlas.wiki.version_store import WikiVersionStore

logger = logging.getLogger(__name__)

# ── Shared Motor client singleton (keyed by URI) ──────────────────────
# Motor opens a connection pool per AsyncIOMotorClient instance; creating
# a new instance on every WikiCache.__init__ call leaks those pools.
# This dict caches one client per unique mongodb_uri, created lazily under
# a module-level lock that is itself created lazily (asyncio.Lock() must be
# created inside a running event-loop on Python <3.10).
_motor_clients: dict[str, AsyncIOMotorClient] = {}
_motor_clients_lock: asyncio.Lock | None = None


def _get_clients_lock() -> asyncio.Lock:
    global _motor_clients_lock
    if _motor_clients_lock is None:
        _motor_clients_lock = asyncio.Lock()
    return _motor_clients_lock


async def _get_motor_client(mongodb_uri: str) -> AsyncIOMotorClient:
    """Return a shared AsyncIOMotorClient for the given URI, creating it once."""
    lock = _get_clients_lock()
    async with lock:
        if mongodb_uri not in _motor_clients:
            _motor_clients[mongodb_uri] = AsyncIOMotorClient(mongodb_uri)
        return _motor_clients[mongodb_uri]


def _cache_key(channel_id: str, target_lang: str) -> str:
    """Return the compound cache key for a channel + language."""
    return f"{channel_id}:{target_lang}"


class WikiCache:
    """Stores one wiki document per channel in MongoDB."""

    def __init__(self, mongodb_uri: str, db_name: str = "beever_atlas") -> None:
        self._mongodb_uri = mongodb_uri
        self._db_name = db_name
        # _db is set lazily via _ensure_db(); callers that need it await that method.
        # For backward compat, synchronous attribute access still works after
        # _ensure_db() has been called at least once (assigned to self._db).
        self._db: Any = None
        self._collection: Any = None
        self._status_collection: Any = None
        self._version_store = WikiVersionStore(mongodb_uri, db_name)

    async def _ensure_db(self) -> None:
        """Resolve the shared Motor client and cache DB/collection handles.

        Short-circuits if _collection has already been set (e.g. by test
        fixtures that inject a mock collection directly).
        """
        if self._collection is not None:
            return
        client = await _get_motor_client(self._mongodb_uri)
        self._db = client[self._db_name]
        self._collection = self._db["wiki_cache"]
        self._status_collection = self._db["wiki_generation_status"]

    @property
    def version_store(self) -> WikiVersionStore:
        return self._version_store

    async def ensure_indexes(self) -> None:
        await self._ensure_db()
        await self._collection.create_index("channel_id", unique=True)
        await self._status_collection.create_index("channel_id", unique=True)
        await self._version_store.ensure_indexes()

    def _is_default_lang(self, target_lang: str) -> bool:
        return target_lang == get_settings().default_target_language

    async def get_wiki(self, channel_id: str, target_lang: str = "en") -> dict | None:
        await self._ensure_db()
        key = _cache_key(channel_id, target_lang)
        doc = await self._collection.find_one({"channel_id": key}, {"_id": 0})
        # Backward-compat: fall back to legacy key for default language
        if doc is None and self._is_default_lang(target_lang):
            doc = await self._collection.find_one({"channel_id": channel_id}, {"_id": 0})
        if doc is not None:
            doc["version_count"] = await self._version_store.count_versions(channel_id)
        return doc

    async def get_page(self, channel_id: str, page_id: str, target_lang: str = "en") -> dict | None:
        await self._ensure_db()
        # When PER_PAGE_WIKI=True, read from the new wiki_pages
        # collection. Falls back to the legacy monolith doc when the
        # per-page row is missing — migration may run lazily / only on
        # first save_page call, so during the soak window pages can
        # exist in either store. The fallback eliminates a window where
        # the UI would 404 mid-migration.
        #
        # First-sync seed compat: ``WikiBuilder.refresh_wiki`` now seeds
        # ``wiki_pages`` with the persistence shape (no ``content``,
        # ``modules``, or ``narrative_sections`` fields — those are
        # render-only and live on the ``wiki_cache`` blob). Returning the
        # seed alone breaks ``TopicPage``/``OverviewPage`` which do
        # ``page.content.replace(...)``. So when both stores have an
        # entry, MERGE them: persistence row provides metadata (kind,
        # is_dirty, last_facts_seen, …), the legacy cache provides the
        # render-only fields (content, modules, narrative_sections, …).
        # Cache-only fields are layered on top, so a maintainer rewrite
        # that bumps ``version`` in wiki_pages keeps that bump while
        # still surfacing the rendered content the UI needs.
        per_page_row: dict | None = None
        if get_settings().per_page_wiki:
            try:
                from beever_atlas.wiki.page_store import WikiPageStore

                page_store = WikiPageStore(db=self._db)
                page = await page_store.get_page(
                    channel_id=channel_id,
                    page_id=page_id,
                    target_lang=target_lang,
                )
                if page is not None:
                    per_page_row = page.model_dump(mode="json")
            except Exception as exc:  # noqa: BLE001 — fall through to legacy on any error
                logger.warning(
                    "WikiCache.get_page: per-page lookup failed, falling back "
                    "to legacy schema channel=%s page=%s exc=%s",
                    channel_id,
                    page_id,
                    type(exc).__name__,
                )

        key = _cache_key(channel_id, target_lang)
        doc = await self._collection.find_one(
            {"channel_id": key},
            {"_id": 0, f"pages.{page_id}": 1},
        )
        # Backward-compat: fall back to legacy key for default language
        if doc is None and self._is_default_lang(target_lang):
            doc = await self._collection.find_one(
                {"channel_id": channel_id},
                {"_id": 0, f"pages.{page_id}": 1},
            )
        cache_row = doc.get("pages", {}).get(page_id) if doc is not None else None

        if per_page_row is None and cache_row is None:
            return None
        if per_page_row is None:
            return cache_row
        if cache_row is None:
            return per_page_row
        # Both stores have an entry — overlay the legacy cache's
        # render-only fields onto the persistence row. The persistence
        # row stays authoritative for maintenance metadata (kind,
        # is_dirty, last_facts_seen, version, curation_mode, …) which
        # the maintainer mutates on every rewrite. Render-only fields
        # are listed explicitly so a future code path that happens to
        # write ``kind`` or ``version`` into the legacy blob cannot
        # silently clobber the persistence row's value.
        _RENDER_ONLY_KEYS = (
            "content",
            "modules",
            "narrative_sections",
            "summary",
            "memory_count",
            "citations",
            "children",
            "children_fingerprint",
            "section_number",
            "last_updated",
            "page_type",
        )
        merged: dict = dict(per_page_row)
        for k in _RENDER_ONLY_KEYS:
            if k in cache_row:
                merged[k] = cache_row[k]
        return merged

    async def get_structure(self, channel_id: str, target_lang: str = "en") -> dict | None:
        await self._ensure_db()
        key = _cache_key(channel_id, target_lang)
        doc = await self._collection.find_one(
            {"channel_id": key},
            {"_id": 0, "channel_id": 1, "generated_at": 1, "is_stale": 1, "structure": 1},
        )
        # Backward-compat: fall back to legacy key for default language
        if doc is None and self._is_default_lang(target_lang):
            doc = await self._collection.find_one(
                {"channel_id": channel_id},
                {"_id": 0, "channel_id": 1, "generated_at": 1, "is_stale": 1, "structure": 1},
            )
        return doc

    async def save_wiki(self, channel_id: str, wiki_data: dict, target_lang: str = "en") -> None:
        await self._ensure_db()
        key = _cache_key(channel_id, target_lang)
        # One-shot legacy migration: for the default language, if the new-key
        # doc is absent but the legacy (unsuffixed) doc exists, copy the
        # legacy doc to the new key so archival history is preserved.
        if self._is_default_lang(target_lang):
            new_existing = await self._collection.find_one({"channel_id": key}, {"_id": 0})
            if new_existing is None:
                legacy = await self._collection.find_one({"channel_id": channel_id}, {"_id": 0})
                if legacy is not None:
                    legacy["channel_id"] = key
                    try:
                        await self._collection.update_one(
                            {"channel_id": key},
                            {"$set": legacy},
                            upsert=True,
                        )
                    except Exception:
                        logger.exception(
                            "Failed to migrate legacy wiki doc for channel %s",
                            channel_id,
                        )
        # Archive the current wiki before overwriting
        try:
            existing = await self._collection.find_one({"channel_id": key}, {"_id": 0})
            if existing:
                # Preserve the language the archived version was rendered in,
                # so version history can label each entry correctly.
                archived_lang = existing.get("target_lang") or target_lang
                await self._version_store.archive(
                    channel_id,
                    existing,
                    target_lang=archived_lang,
                )
                await self._version_store.cleanup(channel_id)
        except Exception:
            logger.exception("Failed to archive wiki version for channel %s", channel_id)

        # Stamp the live wiki doc with its target_lang so future archive calls
        # can read it back to label older versions correctly. Force the stored
        # ``channel_id`` to the suffixed cache key — otherwise the raw id from
        # the builder output clashes with other-language rows on the unique
        # ``channel_id_1`` index (E11000 duplicate key).
        wiki_data = {**wiki_data, "channel_id": key, "target_lang": target_lang}
        await self._collection.update_one(
            {"channel_id": key},
            {"$set": wiki_data},
            upsert=True,
        )

    async def delete_wiki(self, channel_id: str, target_lang: str = "en") -> bool:
        """Delete the cached wiki document for ``(channel_id, target_lang)``.

        Returns ``True`` if a row was deleted. Does NOT touch the version
        archive — callers that want to preserve a rollback point must
        snapshot via ``version_store.archive`` BEFORE calling this. Used
        by the ``mode=rebuild`` path on POST /wiki/refresh to clear all
        curation flags (pinned/hidden) + folder structure before a fresh
        regeneration so the builder runs against a clean slate.

        When ``per_page_wiki`` is enabled, individual page rows live in
        a separate ``wiki_pages`` collection (and folder→child links in
        ``wiki_redirects``); a true clean slate also wipes those, otherwise
        the regeneration runs on top of stale per-page docs and the
        rebuild's "from scratch" contract is silently violated.
        """
        await self._ensure_db()
        key = _cache_key(channel_id, target_lang)
        result = await self._collection.delete_one({"channel_id": key})
        # Backward-compat: when target_lang is the default, the legacy
        # row may live under the bare ``channel_id`` instead of the
        # suffixed key. Best-effort delete that too so a rebuild on
        # default-lang channels actually wipes both rows.
        if self._is_default_lang(target_lang):
            await self._collection.delete_one({"channel_id": channel_id})

        # Per-page store wipe — only when the feature flag is on. Wrapped
        # in try/except because a per-page wipe failure should NOT block
        # the rebuild (the monolith cache is the source of truth for the
        # generator; the per-page store is a denormalised mirror that
        # save_wiki repopulates on the next successful generation).
        try:
            if get_settings().per_page_wiki:
                from beever_atlas.wiki.page_store import WikiPageStore

                page_store = WikiPageStore(db=self._db)
                await page_store.delete_all_for_channel(
                    channel_id=channel_id, target_lang=target_lang
                )
        except Exception:
            logger.exception(
                "WikiCache.delete_wiki: per-page wipe failed channel=%s lang=%s — "
                "monolith cache deletion still applied",
                channel_id,
                target_lang,
            )

        return result.deleted_count > 0

    async def mark_stale(self, channel_id: str, target_lang: str | None = None) -> None:
        await self._ensure_db()
        if target_lang is None:
            target_lang = get_settings().default_target_language
        key = _cache_key(channel_id, target_lang)
        await self._collection.update_one(
            {"channel_id": key},
            {"$set": {"is_stale": True}},
        )

    async def clear_stale(self, channel_id: str, target_lang: str | None = None) -> None:
        await self._ensure_db()
        if target_lang is None:
            target_lang = get_settings().default_target_language
        key = _cache_key(channel_id, target_lang)
        await self._collection.update_one(
            {"channel_id": key},
            {"$set": {"is_stale": False}},
        )

    async def mark_all_stale(self, channel_id: str) -> None:
        """Mark every language variant of a channel's wiki as stale.

        Matches the bare legacy key (`channel_id`) and any namespaced key
        (`channel_id:<lang>`) in one update.
        """
        await self._ensure_db()
        import re

        pattern = f"^{re.escape(channel_id)}(:.+)?$"
        await self._collection.update_many(
            {"channel_id": {"$regex": pattern}},
            {"$set": {"is_stale": True}},
        )

    # ── Generation status tracking ─────────────────────────────────────

    async def set_generation_status(
        self,
        channel_id: str,
        status: str,
        stage: str,
        stage_detail: str = "",
        pages_total: int = 0,
        pages_done: int = 0,
        pages_completed: list[str] | None = None,
        model: str = "",
        error: str | None = None,
        target_lang: str = "en",
    ) -> None:
        """Upsert the current generation status for a channel."""
        await self._ensure_db()
        key = _cache_key(channel_id, target_lang)
        doc: dict[str, Any] = {
            "channel_id": key,
            "status": status,
            "stage": stage,
            "stage_detail": stage_detail,
            "pages_total": pages_total,
            "pages_done": pages_done,
            "pages_completed": pages_completed or [],
            "model": model,
            "error": error,
            "updated_at": datetime.now(tz=UTC).isoformat(),
        }
        if status == "running" and stage == "gathering":
            doc["started_at"] = datetime.now(tz=UTC).isoformat()
        await self._status_collection.update_one(
            {"channel_id": key},
            {"$set": doc},
            upsert=True,
        )

    async def get_generation_status(self, channel_id: str, target_lang: str = "en") -> dict | None:
        await self._ensure_db()
        key = _cache_key(channel_id, target_lang)
        doc = await self._status_collection.find_one({"channel_id": key}, {"_id": 0})
        # Backward-compat: fall back to legacy key for default language
        if doc is None and self._is_default_lang(target_lang):
            doc = await self._status_collection.find_one({"channel_id": channel_id}, {"_id": 0})
        return doc

    async def clear_generation_status(self, channel_id: str, target_lang: str = "en") -> None:
        await self._ensure_db()
        key = _cache_key(channel_id, target_lang)
        await self._status_collection.delete_one({"channel_id": key})

    async def delete_all_for_channel(self, channel_id: str) -> int:
        """Hard-delete the rendered wiki blob + generation status for a channel
        across ALL languages — used by the channel hard-purge.

        ``delete_wiki`` only clears one language; a purge must remove every
        ``{channel_id}:{lang}`` row (``_cache_key``) plus the legacy bare
        ``channel_id`` row. Both are matched with an end-anchored prefix
        (``^<id>(:.+)?$`` — same shape as ``mark_all_stale``) so an id can never
        collide with another that merely shares a prefix (``C1`` must not match
        ``C12``). Returns total rows deleted across ``wiki_cache`` +
        ``wiki_generation_status``. Does NOT touch ``wiki_pages`` — the purge
        fan-out deletes those via ``WikiPageStore.delete_all_for_channel_all_langs``.
        """
        await self._ensure_db()
        pattern = {"$regex": f"^{re.escape(channel_id)}(:.+)?$"}
        deleted = 0
        r1 = await self._collection.delete_many({"channel_id": pattern})
        deleted += int(r1.deleted_count or 0)
        r2 = await self._status_collection.delete_many({"channel_id": pattern})
        deleted += int(r2.deleted_count or 0)
        return deleted

    def close(self) -> None:
        # The Motor client is now a module-level singleton; do not close it
        # here — closing it would break all other WikiCache instances sharing
        # the same URI. This method is kept for API compatibility only.
        pass
