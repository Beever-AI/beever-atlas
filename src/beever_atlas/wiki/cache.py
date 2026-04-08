"""MongoDB-backed cache for compiled wiki documents."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)


class WikiCache:
    """Stores one wiki document per channel in MongoDB."""

    def __init__(self, mongodb_uri: str, db_name: str = "beever_atlas") -> None:
        self._db = AsyncIOMotorClient(mongodb_uri)[db_name]
        self._collection = self._db["wiki_cache"]
        self._status_collection = self._db["wiki_generation_status"]

    async def ensure_indexes(self) -> None:
        await self._collection.create_index("channel_id", unique=True)
        await self._status_collection.create_index("channel_id", unique=True)

    async def get_wiki(self, channel_id: str) -> dict | None:
        return await self._collection.find_one(
            {"channel_id": channel_id}, {"_id": 0}
        )

    async def get_page(self, channel_id: str, page_id: str) -> dict | None:
        doc = await self._collection.find_one(
            {"channel_id": channel_id},
            {"_id": 0, f"pages.{page_id}": 1},
        )
        if doc is None:
            return None
        return doc.get("pages", {}).get(page_id)

    async def get_structure(self, channel_id: str) -> dict | None:
        return await self._collection.find_one(
            {"channel_id": channel_id},
            {"_id": 0, "channel_id": 1, "generated_at": 1, "is_stale": 1, "structure": 1},
        )

    async def save_wiki(self, channel_id: str, wiki_data: dict) -> None:
        await self._collection.update_one(
            {"channel_id": channel_id},
            {"$set": wiki_data},
            upsert=True,
        )

    async def mark_stale(self, channel_id: str) -> None:
        await self._collection.update_one(
            {"channel_id": channel_id},
            {"$set": {"is_stale": True}},
        )

    async def clear_stale(self, channel_id: str) -> None:
        await self._collection.update_one(
            {"channel_id": channel_id},
            {"$set": {"is_stale": False}},
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
    ) -> None:
        """Upsert the current generation status for a channel."""
        doc: dict[str, Any] = {
            "channel_id": channel_id,
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
                {"channel_id": channel_id},
                {"$set": doc},
                upsert=True,
            )
        else:
            await self._status_collection.update_one(
                {"channel_id": channel_id},
                {"$set": doc},
                upsert=True,
            )

    async def get_generation_status(self, channel_id: str) -> dict | None:
        return await self._status_collection.find_one(
            {"channel_id": channel_id}, {"_id": 0}
        )

    async def clear_generation_status(self, channel_id: str) -> None:
        await self._status_collection.delete_one({"channel_id": channel_id})

    def close(self) -> None:
        self._db.client.close()
