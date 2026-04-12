"""MongoDB-backed store for chat history persistence."""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError

logger = logging.getLogger(__name__)

CHAT_HISTORY_TTL_DAYS = 90
MAX_CONTEXT_TURNS = 10


class ChatHistoryStore:
    """Persists Q&A conversation sessions in MongoDB chat_history collection.

    Two schema variants coexist:
      v1 (legacy, channel-scoped): top-level `channel_id`; messages without channel_id
      v2 (session-scoped): no top-level `channel_id`; each message carries channel_id

    TTL index auto-expires documents after 90 days.
    """

    def __init__(self, mongodb_uri: str, db_name: str = "beever_atlas") -> None:
        self._client = AsyncIOMotorClient(mongodb_uri)
        self._db = self._client[db_name]
        self._collection = self._db["chat_history"]

    async def startup(self) -> None:
        """Create indexes including the 90-day TTL index on created_at."""
        await self._collection.create_index("channel_id")
        await self._collection.create_index("user_id")
        await self._collection.create_index("session_id", unique=True)
        await self._collection.create_index(
            "created_at",
            expireAfterSeconds=CHAT_HISTORY_TTL_DAYS * 24 * 3600,
        )
        logger.info("ChatHistoryStore: indexes ensured (TTL=%d days)", CHAT_HISTORY_TTL_DAYS)

    def _now(self) -> datetime:
        return datetime.now(tz=UTC)

    async def create_session(
        self,
        session_id: str,
        channel_id: str,
        user_id: str,
    ) -> dict:
        """Create a v1 session document with top-level channel_id. Idempotent."""
        doc = {
            "session_id": session_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "messages": [],
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        try:
            await self._collection.insert_one(doc)
        except DuplicateKeyError:
            # Session already exists — idempotent no-op
            pass
        return {k: v for k, v in doc.items() if k != "_id"}

    async def create_session_v2(
        self,
        session_id: str,
        user_id: str,
    ) -> dict:
        """Create a v2 session document WITHOUT a top-level channel_id.

        Channels used in this session are tracked per-message and derived at read time.
        Idempotent — safe to call if session exists (only swallows DuplicateKeyError;
        other failures propagate so connection/auth issues aren't masked).
        """
        doc = {
            "session_id": session_id,
            "user_id": user_id,
            "messages": [],
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        try:
            await self._collection.insert_one(doc)
        except DuplicateKeyError:
            # Session already exists — idempotent no-op
            pass
        return {k: v for k, v in doc.items() if k != "_id"}

    async def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        citations: list[dict] | None = None,
        tools_used: list[str] | None = None,
        channel_id: str | None = None,
        thinking: dict | None = None,
        tool_calls: list[dict] | None = None,
    ) -> None:
        """Append a message to an existing session.

        When `channel_id` is provided, it is persisted on the message subdocument
        (v2 schema). Legacy v1 sessions continue to work without it.

        `thinking` persists the assistant's reasoning trace as
        ``{text, duration_ms, truncated}`` so the UI can re-render the
        collapsed "Thought for Xs" disclosure after reload. `tool_calls`
        persists the tool timeline used to produce the answer.
        """
        message: dict = {
            "role": role,
            "content": content,
            "citations": citations or [],
            "tools_used": tools_used or [],
            "timestamp": self._now().isoformat(),
        }
        if channel_id is not None:
            message["channel_id"] = channel_id
        if thinking is not None:
            message["thinking"] = thinking
        if tool_calls:
            message["tool_calls"] = tool_calls

        await self._collection.update_one(
            {"session_id": session_id},
            {
                "$push": {"messages": message},
                "$set": {"updated_at": self._now()},
            },
        )

    async def load_session(self, session_id: str) -> dict | None:
        """Load a full session by ID. Returns None if not found."""
        return await self._collection.find_one(
            {"session_id": session_id}, {"_id": 0}
        )

    async def load_session_with_channels(self, session_id: str) -> dict | None:
        """Load session and attach a derived `channel_ids` array.

        Aggregates distinct `channel_id`s from messages. For legacy v1 sessions
        that lack per-message `channel_id`, falls back to the top-level
        `channel_id` so each message appears to have one.
        """
        doc = await self._collection.find_one({"session_id": session_id}, {"_id": 0})
        if doc is None:
            return None

        legacy_channel_id = doc.get("channel_id")
        channel_ids: list[str] = []
        seen: set[str] = set()
        for msg in doc.get("messages", []):
            cid = msg.get("channel_id") or legacy_channel_id
            if cid and cid not in seen:
                seen.add(cid)
                channel_ids.append(cid)
            # Also fill in channel_id on legacy messages for frontend convenience
            if "channel_id" not in msg and legacy_channel_id:
                msg["channel_id"] = legacy_channel_id

        doc["channel_ids"] = channel_ids
        return doc

    async def get_context_messages(self, session_id: str) -> list[dict]:
        """Return the last MAX_CONTEXT_TURNS messages for agent context window."""
        doc = await self._collection.find_one(
            {"session_id": session_id},
            {"_id": 0, "messages": {"$slice": -MAX_CONTEXT_TURNS}},
        )
        if doc is None:
            return []
        return doc.get("messages", [])

    async def list_sessions(
        self,
        channel_id: str,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> list[dict]:
        """Return paginated v1 sessions for a user+channel, newest first.

        Only returns sessions whose top-level channel_id matches (legacy behavior).
        """
        skip = (page - 1) * page_size
        cursor = (
            self._collection.find(
                {"channel_id": channel_id, "user_id": user_id},
                {
                    "_id": 0,
                    "session_id": 1,
                    "created_at": 1,
                    "messages": {"$slice": 1},
                },
            )
            .sort("created_at", -1)
            .skip(skip)
            .limit(page_size)
        )
        results = []
        async for doc in cursor:
            first_q = ""
            msgs = doc.get("messages", [])
            if msgs:
                first_q = msgs[0].get("content", "")[:120]
            created = doc.get("created_at")
            results.append({
                "session_id": doc["session_id"],
                "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
                "first_question": first_q,
            })
        return results

    async def list_sessions_global(
        self,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
        search: str | None = None,
    ) -> list[dict]:
        """Return paginated sessions for a user across ALL channels, newest first.

        Excludes soft-deleted sessions. Supports optional substring search over
        title and message content. Each entry includes a derived `channel_ids`
        array so the frontend can render per-session channel badges.
        """
        skip = (page - 1) * page_size

        query: dict = {
            "user_id": user_id,
            "is_deleted": {"$ne": True},
        }
        if search:
            escaped = re.escape(search)
            query["$or"] = [
                {"title": {"$regex": escaped, "$options": "i"}},
                {"messages.content": {"$regex": escaped, "$options": "i"}},
            ]

        # Include messages so we can derive channel_ids; strip heavy fields at Python level.
        cursor = (
            self._collection.find(
                query,
                {
                    "_id": 0,
                    "session_id": 1,
                    "created_at": 1,
                    "title": 1,
                    "pinned": 1,
                    "channel_id": 1,
                    "messages.content": 1,
                    "messages.channel_id": 1,
                },
            )
            .sort("created_at", -1)
            .skip(skip)
            .limit(page_size)
        )

        results: list[dict] = []
        async for doc in cursor:
            msgs = doc.get("messages") or []
            first_q = ""
            if msgs:
                first_q = (msgs[0].get("content") or "")[:120]

            legacy_channel = doc.get("channel_id")
            seen: set[str] = set()
            channel_ids: list[str] = []
            for m in msgs:
                cid = m.get("channel_id") or legacy_channel
                if cid and cid not in seen:
                    seen.add(cid)
                    channel_ids.append(cid)

            created = doc.get("created_at")
            results.append({
                "session_id": doc["session_id"],
                "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
                "first_question": first_q,
                "title": doc.get("title"),
                "pinned": doc.get("pinned", False),
                "channel_ids": channel_ids,
            })
        return results

    def close(self) -> None:
        self._client.close()
