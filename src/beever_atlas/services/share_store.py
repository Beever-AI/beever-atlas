"""MongoDB-backed store for shared conversation snapshots.

Mirrors the shape of `ChatHistoryStore` — async Motor client, `startup()`
ensures indexes, `close()` for teardown. One document per (owner_user_id,
source_session_id) active share; rotation is atomic via single-document
`find_one_and_update`.
"""

from __future__ import annotations

import logging
import os
import secrets
from datetime import UTC, datetime
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

logger = logging.getLogger(__name__)

ACCESS_LOG_CAP = 1000


def generate_share_token() -> str:
    """256-bit URL-safe token (43 chars)."""
    return secrets.token_urlsafe(32)


class ShareStore:
    """CRUD for the `shared_conversations` collection."""

    def __init__(self, mongodb_uri: str, db_name: str | None = None) -> None:
        resolved = db_name or os.environ.get("BEEVER_CHAT_HISTORY_DB", "beever_atlas")
        self._client = AsyncIOMotorClient(mongodb_uri)
        self._db = self._client[resolved]
        self._collection = self._db["shared_conversations"]

    async def startup(self) -> None:
        await self._collection.create_index("share_token", unique=True)
        await self._collection.create_index(
            [("owner_user_id", 1), ("source_session_id", 1)],
            name="owner_source_idx",
        )
        logger.info("ShareStore: indexes ensured")

    def close(self) -> None:
        self._client.close()

    @staticmethod
    def _now() -> datetime:
        return datetime.now(tz=UTC)

    async def get_active_by_session(
        self, owner_user_id: str, source_session_id: str
    ) -> dict | None:
        return await self._collection.find_one(
            {
                "owner_user_id": owner_user_id,
                "source_session_id": source_session_id,
                "revoked_at": None,
            }
        )

    async def get_by_token(self, share_token: str) -> dict | None:
        return await self._collection.find_one({"share_token": share_token})

    async def create(
        self,
        *,
        owner_user_id: str,
        source_session_id: str,
        visibility: str,
        title: str,
        messages: list[dict],
    ) -> dict:
        now = self._now()
        doc = {
            "share_token": generate_share_token(),
            "owner_user_id": owner_user_id,
            "source_session_id": source_session_id,
            "visibility": visibility,
            "title": title,
            "messages": messages,
            "created_at": now,
            "updated_at": now,
            "rotated_at": None,
            "revoked_at": None,
            "access_log": [],
        }
        result = await self._collection.insert_one(doc)
        doc["_id"] = result.inserted_id
        return doc

    async def rotate_token(self, doc_id: Any) -> dict | None:
        """Atomic token rotation — single `findOneAndUpdate` guarded by
        `revoked_at IS NULL` so a concurrent rotate on an already-rotated
        doc still flips only once (the new token wins; losers see the
        previous state but their old token is invalidated).
        """
        now = self._now()
        new_token = generate_share_token()
        updated = await self._collection.find_one_and_update(
            {"_id": doc_id, "revoked_at": None},
            {
                "$set": {
                    "share_token": new_token,
                    "rotated_at": now,
                    "updated_at": now,
                }
            },
            return_document=True,
        )
        return updated

    async def resnapshot(self, doc_id: Any, *, title: str, messages: list[dict]) -> dict | None:
        now = self._now()
        return await self._collection.find_one_and_update(
            {"_id": doc_id, "revoked_at": None},
            {
                "$set": {
                    "messages": messages,
                    "title": title,
                    "updated_at": now,
                }
            },
            return_document=True,
        )

    async def update_visibility(self, doc_id: Any, visibility: str) -> dict | None:
        now = self._now()
        return await self._collection.find_one_and_update(
            {"_id": doc_id, "revoked_at": None},
            {"$set": {"visibility": visibility, "updated_at": now}},
            return_document=True,
        )

    async def revoke(self, doc_id: Any) -> bool:
        """Set revoked_at. Returns True iff this call transitioned state."""
        now = self._now()
        result = await self._collection.update_one(
            {"_id": doc_id, "revoked_at": None},
            {"$set": {"revoked_at": now, "updated_at": now}},
        )
        return result.modified_count == 1

    async def append_access_log(self, doc_id: Any, entry: dict) -> None:
        """FIFO append with cap ACCESS_LOG_CAP via `$slice`."""
        await self._collection.update_one(
            {"_id": doc_id},
            {"$push": {"access_log": {"$each": [entry], "$slice": -ACCESS_LOG_CAP}}},
        )
