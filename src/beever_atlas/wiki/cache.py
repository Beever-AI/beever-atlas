"""MongoDB-backed cache for compiled wiki documents."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from beever_atlas.infra.config import get_settings
from beever_atlas.wiki.version_store import WikiVersionStore

logger = logging.getLogger(__name__)


def _cache_key(channel_id: str, target_lang: str) -> str:
    """Return the compound cache key for a channel + language."""
    return f"{channel_id}:{target_lang}"


class WikiCache:
    """Stores one wiki document per channel in MongoDB."""

    def __init__(self, mongodb_uri: str, db_name: str = "beever_atlas") -> None:
        self._db = AsyncIOMotorClient(mongodb_uri)[db_name]
        self._collection = self._db["wiki_cache"]
        self._status_collection = self._db["wiki_generation_status"]
        self._version_store = WikiVersionStore(mongodb_uri, db_name)

    @property
    def version_store(self) -> WikiVersionStore:
        return self._version_store

    async def ensure_indexes(self) -> None:
        await self._collection.create_index("channel_id", unique=True)
        await self._status_collection.create_index("channel_id", unique=True)
        await self._version_store.ensure_indexes()

    def _is_default_lang(self, target_lang: str) -> bool:
        return target_lang == get_settings().default_target_language

    async def get_wiki(self, channel_id: str, target_lang: str = "en") -> dict | None:
        key = _cache_key(channel_id, target_lang)
        doc = await self._collection.find_one({"channel_id": key}, {"_id": 0})
        # Backward-compat: fall back to legacy key for default language
        if doc is None and self._is_default_lang(target_lang):
            doc = await self._collection.find_one({"channel_id": channel_id}, {"_id": 0})
        if doc is not None:
            doc["version_count"] = await self._version_store.count_versions(channel_id)
        return doc

    async def get_page(self, channel_id: str, page_id: str, target_lang: str = "en") -> dict | None:
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
        if doc is None:
            return None
        return doc.get("pages", {}).get(page_id)

    async def get_structure(self, channel_id: str, target_lang: str = "en") -> dict | None:
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
        key = _cache_key(channel_id, target_lang)
        # One-shot legacy migration: for the default language, if the new-key
        # doc is absent but the legacy (unsuffixed) doc exists, copy the
        # legacy doc to the new key so archival history is preserved.
        if self._is_default_lang(target_lang):
            new_existing = await self._collection.find_one(
                {"channel_id": key}, {"_id": 0}
            )
            if new_existing is None:
                legacy = await self._collection.find_one(
                    {"channel_id": channel_id}, {"_id": 0}
                )
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
                await self._version_store.archive(channel_id, existing)
                await self._version_store.cleanup(channel_id)
        except Exception:
            logger.exception("Failed to archive wiki version for channel %s", channel_id)

        await self._collection.update_one(
            {"channel_id": key},
            {"$set": wiki_data},
            upsert=True,
        )

    async def mark_stale(self, channel_id: str, target_lang: str | None = None) -> None:
        if target_lang is None:
            target_lang = get_settings().default_target_language
        key = _cache_key(channel_id, target_lang)
        await self._collection.update_one(
            {"channel_id": key},
            {"$set": {"is_stale": True}},
        )

    async def clear_stale(self, channel_id: str, target_lang: str | None = None) -> None:
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
        key = _cache_key(channel_id, target_lang)
        doc = await self._status_collection.find_one({"channel_id": key}, {"_id": 0})
        # Backward-compat: fall back to legacy key for default language
        if doc is None and self._is_default_lang(target_lang):
            doc = await self._status_collection.find_one(
                {"channel_id": channel_id}, {"_id": 0}
            )
        return doc

    async def clear_generation_status(self, channel_id: str, target_lang: str = "en") -> None:
        key = _cache_key(channel_id, target_lang)
        await self._status_collection.delete_one({"channel_id": key})

    def close(self) -> None:
        self._db.client.close()
