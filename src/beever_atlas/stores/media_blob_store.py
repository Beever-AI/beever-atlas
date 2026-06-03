"""Content-addressed durable blob store for channel media (GridFS).

Channel media (Slack/Discord/Mattermost/Teams images, PDFs, videos) is
referenced everywhere by its platform CDN URL, but those URLs rot — Discord
signed URLs expire (~24 h), and Slack/Mattermost/Teams URLs need a live bot
token forever. This store keeps a durable copy of the raw bytes so the
read-through proxy can serve them after the platform URL has died.

Two backing collections:

  * GridFS bucket ``channel_media`` (``channel_media.files`` +
    ``channel_media.chunks``) — the raw bytes. Files are keyed by the
    sha256 of the RAW bytes (no version salt) and deduped per
    ``(sha256, channel_id)`` so the same image shared in two channels is
    stored once per channel (channel scoping keeps purge simple).
  * ``channel_media_refs`` — maps a normalized ``url_key`` (host+path,
    lowercase host, no scheme/query/fragment) to the stored blob:
        {
            "url_key":     str,    # stable identity across re-signs
            "channel_id":  str,
            "sha256":      str,    # -> GridFS metadata.sha256
            "message_id":  str,
            "source_id":   str,
            "mime_type":   str,
            "filename":    str,
            "size_bytes":  int,
            "created_at":  datetime,
            "updated_at":  datetime,
        }

``url_key`` is host+path on purpose: query params are auth/signature
material on every platform (Discord ``ex/is/hm`` signatures, Slack tokens)
and rotate between syncs, so an expired URL still resolves to the stored
bytes because its path is unchanged. Telegram is the exception — its file
URLs embed the bot token in the *path* (``api.telegram.org/file/bot<TOKEN>/…``)
so a Telegram host yields an empty ``url_key`` and we never index a ref for it.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from motor.motor_asyncio import AsyncIOMotorGridFSBucket

if TYPE_CHECKING:  # pragma: no cover
    from motor.motor_asyncio import (
        AsyncIOMotorCollection,
        AsyncIOMotorDatabase,
        AsyncIOMotorGridOut,
    )

    from beever_atlas.stores.mongodb_store import MongoDBStore

logger = logging.getLogger(__name__)

# Bucket name groups the chunks + files collections so the channel-purge
# fan-out and the admin metrics endpoint can target them cleanly.
_BUCKET = "channel_media"
# Telegram file URLs carry the bot token in the path — never index them.
_TELEGRAM_HOST = "api.telegram.org"


class MediaBlobStore:
    """Durable, content-addressed store for channel media bytes.

    Reuses the shared Motor client from ``MongoDBStore`` (PlatformStore
    pattern) instead of opening its own pool, so the GridFS bucket and the
    ``channel_media_refs`` collection live on the same connection as the
    rest of the app.
    """

    def __init__(self, mongodb: "MongoDBStore | AsyncIOMotorDatabase") -> None:
        # Accept either the MongoDBStore (preferred — exposes ``.db``) or a
        # raw database handle so tests can inject a fake db directly.
        db = getattr(mongodb, "db", mongodb)
        self._db: "AsyncIOMotorDatabase" = db
        self._bucket: AsyncIOMotorGridFSBucket | None = None
        self._refs: "AsyncIOMotorCollection | None" = None

    async def startup(self) -> None:
        """Bind the bucket + refs collection and create indexes.

        Index creation is idempotent and failure-tolerant: a transient Mongo
        hiccup at boot must not crash the lifespan, mirroring the other
        stores' ensure-index conventions.
        """
        self._bucket = AsyncIOMotorGridFSBucket(self._db, bucket_name=_BUCKET)
        self._refs = self._db["channel_media_refs"]
        try:
            # Primary identity: one ref per (url_key, channel_id).
            await self._refs.create_index(
                [("url_key", 1), ("channel_id", 1)],
                unique=True,
                name="channel_media_refs_url_channel_unique",
            )
            # Channel-scoped purge scan.
            await self._refs.create_index(
                "channel_id",
                name="channel_media_refs_channel_id",
            )
            # Dedup / backfill lookups by content hash within a channel.
            await self._refs.create_index(
                [("sha256", 1), ("channel_id", 1)],
                name="channel_media_refs_sha_channel",
            )
            # GridFS files collection: the dedup probe (save_blob), the
            # content-hash read (open_by_hash), and the channel purge scan
            # (delete_by_channel) all query ``channel_media.files`` by
            # ``metadata.{sha256,channel_id}`` — GridFS auto-indexes only
            # filename/uploadDate, never metadata.*, so without these the
            # dedup hot path degrades to a full collection scan as the store
            # grows. The compound index covers the (sha256, channel_id)
            # lookups; the single-field index covers the purge scan.
            files_col = self._db[f"{_BUCKET}.files"]
            await files_col.create_index(
                [("metadata.sha256", 1), ("metadata.channel_id", 1)],
                name="channel_media_files_sha_channel",
            )
            await files_col.create_index(
                "metadata.channel_id",
                name="channel_media_files_channel_id",
            )
        except Exception as exc:  # pragma: no cover - defensive boot path
            logger.warning(
                "MediaBlobStore: index creation failed (continuing) error=%s",
                exc,
            )

    def _ensure_ready(self) -> tuple[AsyncIOMotorGridFSBucket, "AsyncIOMotorCollection"]:
        if self._bucket is None or self._refs is None:
            raise RuntimeError("MediaBlobStore.startup() was not called")
        return self._bucket, self._refs

    # ------------------------------------------------------------------
    # URL normalization
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_url_key(url: str) -> str:
        """Return the stable ``host+path`` identity for a media URL.

        Lowercases the host, drops scheme/query/fragment/port-cosmetics
        beyond what ``urlparse`` gives. An empty or unparseable URL yields
        ``""``; a Telegram host yields ``""`` too so its token-bearing path
        is never indexed.
        """
        if not url:
            return ""
        try:
            parsed = urlparse(url)
        except Exception:
            return ""
        host = (parsed.hostname or "").lower()
        if not host:
            return ""
        if host == _TELEGRAM_HOST:
            return ""
        return f"{host}{parsed.path}"

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def save_blob(
        self,
        *,
        content: bytes,
        mime_type: str,
        filename: str,
        channel_id: str,
        source_id: str,
        message_id: str,
        platform_url: str,
    ) -> str:
        """Persist ``content`` and upsert its ref; return the sha256 hex.

        Best-effort by contract — the caller treats persistence as a side
        effect that must never block extraction. The sha256 is always
        returned, even when the blob is rejected (oversized) or only the
        ref is skipped (empty ``url_key``), so callers have a stable handle.

        Dedup: if a GridFS file already carries ``{sha256, channel_id}`` the
        upload is skipped. The ref is keyed ``(url_key, channel_id)`` and
        upserted regardless so a re-signed URL refreshes its mapping.
        """
        bucket, refs = self._ensure_ready()
        sha256 = hashlib.sha256(content).hexdigest()
        size_bytes = len(content)

        # Size guard — read lazily so test settings overrides apply.
        from beever_atlas.infra.config import get_settings

        max_bytes = get_settings().media_max_file_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            logger.warning(
                "MediaBlobStore: skipping oversized blob sha256=%s size=%d max=%d channel=%s",
                sha256,
                size_bytes,
                max_bytes,
                channel_id,
            )
            return sha256

        url_key = self.normalize_url_key(platform_url)

        # Dedup: skip the GridFS upload if (sha256, channel_id) already exists.
        try:
            existing = await self._db[f"{_BUCKET}.files"].find_one(
                {"metadata.sha256": sha256, "metadata.channel_id": channel_id},
                projection={"_id": 1},
            )
        except Exception as exc:
            logger.warning(
                "MediaBlobStore: dedup lookup failed sha256=%s channel=%s error=%s",
                sha256,
                channel_id,
                exc,
            )
            existing = None

        if existing is None:
            metadata = {
                "sha256": sha256,
                "channel_id": channel_id,
                "source_id": source_id,
                "mime_type": mime_type,
            }
            stream = bucket.open_upload_stream(filename, metadata=metadata)
            try:
                await stream.write(content)
            finally:
                await stream.close()
            logger.info(
                "MediaBlobStore: stored blob sha256=%s channel=%s size=%d mime=%s",
                sha256,
                channel_id,
                size_bytes,
                mime_type,
            )

        # Telegram / invalid URL — store the blob but never index a ref.
        if not url_key:
            return sha256

        now = datetime.now(UTC)
        await refs.update_one(
            {"url_key": url_key, "channel_id": channel_id},
            {
                "$set": {
                    "sha256": sha256,
                    "message_id": message_id,
                    "source_id": source_id,
                    "mime_type": mime_type,
                    "filename": filename,
                    "size_bytes": size_bytes,
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "url_key": url_key,
                    "channel_id": channel_id,
                    "created_at": now,
                },
            },
            upsert=True,
        )
        return sha256

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    async def open_by_url(self, url: str) -> "tuple[Any, dict] | None":
        """Open the stored stream for a platform URL, or ``None`` on miss.

        Channel-agnostic: the proxy doesn't know the channel, so we match
        ANY ref carrying the URL's ``url_key`` and open the blob for that
        ref's ``(sha256, channel_id)``. Returns ``(stream, ref_doc)`` on
        hit. A genuine miss and a store error both return ``None`` (the
        proxy must stay resilient), but a store error is logged at WARN so
        it's distinguishable from an empty/Telegram URL (which returns early).
        """
        url_key = self.normalize_url_key(url)
        if not url_key:
            return None
        _, refs = self._ensure_ready()
        try:
            ref = await refs.find_one({"url_key": url_key})
        except Exception as exc:
            logger.warning("MediaBlobStore: ref lookup failed url_key=%s error=%s", url_key, exc)
            return None
        if ref is None:
            return None
        stream = await self.open_by_hash(ref["sha256"], ref["channel_id"])
        if stream is None:
            return None
        return stream, ref

    async def open_by_hash(self, sha256: str, channel_id: str) -> "AsyncIOMotorGridOut | None":
        """Open the GridFS download stream for ``(sha256, channel_id)``."""
        bucket, _ = self._ensure_ready()
        try:
            file_doc = await self._db[f"{_BUCKET}.files"].find_one(
                {"metadata.sha256": sha256, "metadata.channel_id": channel_id},
                projection={"_id": 1},
            )
            if file_doc is None:
                return None
            return await bucket.open_download_stream(file_doc["_id"])
        except Exception as exc:
            logger.warning(
                "MediaBlobStore: open_by_hash failed sha256=%s channel=%s error=%s",
                sha256,
                channel_id,
                exc,
            )
            return None

    async def has_ref(self, url: str, channel_id: str) -> bool:
        """Cheap existence check for backfill idempotency."""
        url_key = self.normalize_url_key(url)
        if not url_key:
            return False
        _, refs = self._ensure_ready()
        try:
            doc = await refs.find_one(
                {"url_key": url_key, "channel_id": channel_id},
                projection={"_id": 1},
            )
        except Exception as exc:
            logger.warning(
                "MediaBlobStore: has_ref lookup failed url_key=%s channel=%s error=%s",
                url_key,
                channel_id,
                exc,
            )
            return False
        return doc is not None

    # ------------------------------------------------------------------
    # Purge
    # ------------------------------------------------------------------

    async def delete_by_channel(self, channel_id: str) -> dict[str, int]:
        """Delete every blob + ref for ``channel_id``; return the counts.

        Runs inside the channel-purge fan-out, so it must NEVER raise — on a
        partial failure it logs and returns the counts achieved so far.
        Blobs are deleted one-by-one via ``bucket.delete(_id)`` (the only
        way to drop both the file doc and its chunks) over the file docs
        whose ``metadata.channel_id`` matches; refs are dropped in one
        ``delete_many``.
        """
        bucket, refs = self._ensure_ready()
        blobs_deleted = 0
        refs_deleted = 0

        try:
            files_col = self._db[f"{_BUCKET}.files"]
            async for file_doc in files_col.find(
                {"metadata.channel_id": channel_id}, projection={"_id": 1}
            ):
                try:
                    await bucket.delete(file_doc["_id"])
                    blobs_deleted += 1
                except Exception as exc:
                    logger.warning(
                        "MediaBlobStore: blob delete failed channel=%s id=%s error=%s",
                        channel_id,
                        file_doc.get("_id"),
                        exc,
                    )
        except Exception as exc:
            logger.warning(
                "MediaBlobStore: blob purge scan failed channel=%s error=%s",
                channel_id,
                exc,
            )

        try:
            result = await refs.delete_many({"channel_id": channel_id})
            refs_deleted = int(getattr(result, "deleted_count", 0) or 0)
        except Exception as exc:
            logger.warning("MediaBlobStore: ref purge failed channel=%s error=%s", channel_id, exc)

        logger.info(
            "MediaBlobStore: purged channel=%s blobs=%d refs=%d",
            channel_id,
            blobs_deleted,
            refs_deleted,
        )
        return {"blobs_deleted": blobs_deleted, "refs_deleted": refs_deleted}

    # ------------------------------------------------------------------
    # Metrics
    # ------------------------------------------------------------------

    async def stats(self) -> dict[str, int]:
        """Return cheap totals for the admin metrics endpoint."""
        _, refs = self._ensure_ready()
        files_col = self._db[f"{_BUCKET}.files"]
        total_blobs = 0
        total_bytes = 0
        total_refs = 0
        try:
            total_blobs = await files_col.count_documents({})
            cursor = files_col.aggregate([{"$group": {"_id": None, "bytes": {"$sum": "$length"}}}])
            async for row in cursor:
                total_bytes = int(row.get("bytes", 0) or 0)
            total_refs = await refs.count_documents({})
        except Exception as exc:
            logger.warning("MediaBlobStore: stats failed error=%s", exc)
        return {
            "total_blobs": total_blobs,
            "total_bytes": total_bytes,
            "total_refs": total_refs,
        }
