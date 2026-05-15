"""MongoDB-backed cache for media extractor outputs (P0-2).

Stores SHA-256-keyed descriptions produced by Gemini vision/audio/video
calls so that re-syncs of the same file bytes skip the Gemini round-trip.

Cache schema per document:
    {
        "hash":          str,       # SHA-256 hex of (file_bytes + version_bytes)
        "mime_type":     str,       # e.g. "image/png"
        "description":   str,       # extracted text returned by the extractor
        "model_version": str,       # settings.media_vision_model at extraction time
        "extracted_at":  datetime,  # UTC timestamp
    }

Compound index (hash, mime_type) with unique=True.  TTL is indefinite —
cache-bust by bumping MEDIA_CACHE_VERSION (mixes into the hash key).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Optional

from motor.motor_asyncio import AsyncIOMotorCollection

logger = logging.getLogger(__name__)


@dataclass
class CachedMedia:
    """Minimal projection returned on a cache hit."""

    description: str
    model_version: str


class MediaCacheStore:
    """Thin wrapper around the ``media_cache`` MongoDB collection."""

    def __init__(self, collection: AsyncIOMotorCollection) -> None:
        self._col = collection

    async def ensure_indexes(self) -> None:
        """Create indexes — called once during ``MongoDBStore.startup()``."""
        await self._col.create_index(
            [("hash", 1), ("mime_type", 1)],
            unique=True,
            name="media_cache_hash_mime_unique",
        )

    async def get_cached(self, content_hash: str, mime_type: str) -> Optional[CachedMedia]:
        """Return a cached result or ``None`` on miss."""
        doc = await self._col.find_one(
            {"hash": content_hash, "mime_type": mime_type},
            projection={"description": 1, "model_version": 1, "_id": 0},
        )
        if doc is None:
            return None
        return CachedMedia(
            description=doc["description"],
            model_version=doc.get("model_version", ""),
        )

    async def set_cached(
        self,
        content_hash: str,
        mime_type: str,
        description: str,
        model_version: str,
    ) -> None:
        """Upsert a cache entry.  Idempotent — safe to call multiple times."""
        await self._col.update_one(
            {"hash": content_hash, "mime_type": mime_type},
            {
                "$set": {
                    "hash": content_hash,
                    "mime_type": mime_type,
                    "description": description,
                    "model_version": model_version,
                    "extracted_at": datetime.now(UTC),
                }
            },
            upsert=True,
        )
