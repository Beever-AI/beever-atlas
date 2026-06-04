"""GridFS implementation of :class:`BlobBackend` (the default, zero-infra OSS path).

This reproduces ``MediaBlobStore``'s former GridFS touchpoints verbatim — the
``channel_media`` bucket, the ``metadata.{sha256, channel_id, source_id,
mime_type}`` file-doc shape, the two ``channel_media.files`` metadata indexes,
the dedup probe, the 256 KB-chunked download streamer, the per-file purge loop,
and the count/``$sum:$length`` stats aggregate — but addressed by the new
``channels/{channel_id}/{sha256}`` key. Because the on-disk file docs are
unchanged, existing data and existing integration tests keep resolving.

The blob key is parsed back into ``(channel_id, sha256)`` for the Mongo
metadata queries; ``put`` takes the extra ``filename``/``source_id`` it needs
to stamp the file doc as keyword args (MinIO ignores them).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from motor.motor_asyncio import AsyncIOMotorGridFSBucket

from beever_atlas.stores.blob_backend import BlobRead

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator

    from motor.motor_asyncio import AsyncIOMotorDatabase

logger = logging.getLogger(__name__)

# Bucket name groups the chunks + files collections so the channel-purge
# fan-out and the admin metrics endpoint can target them cleanly.
_BUCKET = "channel_media"
# Download chunk size for the read-through path. Streaming in 256 KB slices
# keeps a large blob (video/PDF) off the heap instead of buffering the whole
# file with a single ``.read()``.
_CHUNK_BYTES = 256 * 1024


def _parse_key(key: str) -> tuple[str, str]:
    """Split ``channels/{channel_id}/{sha256}`` into ``(channel_id, sha256)``.

    ``channel_id`` may itself contain slashes in principle, so we anchor on
    the leading ``channels/`` segment and treat the final segment as the
    sha256, keeping everything between as the channel id.
    """
    rest = key.removeprefix("channels/")
    channel_id, _, sha256 = rest.rpartition("/")
    return channel_id, sha256


def _parse_prefix(prefix: str) -> str:
    """Return the channel id from a ``channels/{channel_id}/`` purge prefix."""
    return prefix.removeprefix("channels/").rstrip("/")


class GridFSBackend:
    """:class:`BlobBackend` over the existing ``channel_media`` GridFS bucket.

    Borrows the shared Motor client (like ``MediaBlobStore`` itself) — it never
    opens its own pool, so :meth:`close` is a no-op.
    """

    def __init__(self, db: "AsyncIOMotorDatabase") -> None:
        self._db = db
        self._bucket: AsyncIOMotorGridFSBucket | None = None

    async def startup(self) -> None:
        """Bind the bucket and create the two ``channel_media.files`` indexes.

        These index ``channel_media.files`` by ``metadata.{sha256,channel_id}``
        (and ``metadata.channel_id`` for the purge scan); GridFS auto-indexes
        only ``filename``/``uploadDate``, never ``metadata.*``, so without them
        the dedup hot path degrades to a full collection scan as the store
        grows. Idempotent + failure-tolerant: a transient Mongo hiccup at boot
        must not crash the lifespan.
        """
        self._bucket = AsyncIOMotorGridFSBucket(self._db, bucket_name=_BUCKET)
        try:
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
                "GridFSBackend: index creation failed (continuing) error=%s",
                exc,
            )

    def _ensure_ready(self) -> AsyncIOMotorGridFSBucket:
        if self._bucket is None:
            raise RuntimeError("GridFSBackend.startup() was not called")
        return self._bucket

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        filename: str = "blob",
        source_id: str = "",
    ) -> None:
        """Store ``data`` under ``key``, stamping the legacy metadata.

        The file doc keeps the exact ``metadata.{sha256, channel_id, source_id,
        mime_type}`` shape so existing on-disk docs and integration tests still
        resolve. No internal dedup probe (C3): ``MediaBlobStore.save_blob``
        already calls ``exists(key)`` and skips ``put`` on a hit, so a second
        probe here is a redundant Mongo round-trip — this matches
        ``MinioBackend.put`` (which has none) and makes a write a single
        round-trip.
        """
        bucket = self._ensure_ready()
        channel_id, sha256 = _parse_key(key)
        metadata = {
            "sha256": sha256,
            "channel_id": channel_id,
            "source_id": source_id,
            "mime_type": content_type,
        }
        stream = bucket.open_upload_stream(filename, metadata=metadata)
        try:
            await stream.write(data)
        finally:
            await stream.close()

    async def open(self, key: str) -> BlobRead | None:
        """Open the GridFS download stream for ``key`` as a neutral byte read."""
        bucket = self._ensure_ready()
        channel_id, sha256 = _parse_key(key)
        try:
            file_doc = await self._db[f"{_BUCKET}.files"].find_one(
                {"metadata.sha256": sha256, "metadata.channel_id": channel_id},
            )
            if file_doc is None:
                return None
            stream = await bucket.open_download_stream(file_doc["_id"])
        except Exception as exc:
            logger.warning(
                "GridFSBackend: open failed sha256=%s channel=%s error=%s",
                sha256,
                channel_id,
                exc,
            )
            return None
        metadata = file_doc.get("metadata") or {}
        return BlobRead(
            iterator=_iter_gridout(stream),
            content_type=metadata.get("mime_type"),
            size=file_doc.get("length"),
        )

    async def exists(self, key: str) -> bool:
        """The ``(sha256, channel_id)`` dedup probe over ``channel_media.files``."""
        channel_id, sha256 = _parse_key(key)
        try:
            existing = await self._db[f"{_BUCKET}.files"].find_one(
                {"metadata.sha256": sha256, "metadata.channel_id": channel_id},
                projection={"_id": 1},
            )
        except Exception as exc:
            logger.warning(
                "GridFSBackend: exists probe failed sha256=%s channel=%s error=%s",
                sha256,
                channel_id,
                exc,
            )
            return False
        return existing is not None

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every blob for the channel encoded in ``prefix``.

        Blobs are deleted one-by-one via ``bucket.delete(_id)`` (the only way
        to drop both the file doc and its chunks) over the file docs whose
        ``metadata.channel_id`` matches. Never raises — logs and returns the
        count achieved so far (the channel-purge fan-out depends on this).
        """
        bucket = self._ensure_ready()
        channel_id = _parse_prefix(prefix)
        deleted = 0
        try:
            files_col = self._db[f"{_BUCKET}.files"]
            async for file_doc in files_col.find(
                {"metadata.channel_id": channel_id}, projection={"_id": 1}
            ):
                try:
                    await bucket.delete(file_doc["_id"])
                    deleted += 1
                except Exception as exc:
                    logger.warning(
                        "GridFSBackend: blob delete failed channel=%s id=%s error=%s",
                        channel_id,
                        file_doc.get("_id"),
                        exc,
                    )
        except Exception as exc:
            logger.warning(
                "GridFSBackend: blob purge scan failed channel=%s error=%s",
                channel_id,
                exc,
            )
        return deleted

    async def stats(self) -> tuple[int, int]:
        """Return ``(total_blobs, total_bytes)`` from ``channel_media.files``."""
        files_col = self._db[f"{_BUCKET}.files"]
        total_blobs = 0
        total_bytes = 0
        try:
            total_blobs = await files_col.count_documents({})
            cursor = files_col.aggregate([{"$group": {"_id": None, "bytes": {"$sum": "$length"}}}])
            async for row in cursor:
                total_bytes = int(row.get("bytes", 0) or 0)
        except Exception as exc:
            logger.warning("GridFSBackend: stats failed error=%s", exc)
        return total_blobs, total_bytes

    async def close(self) -> None:
        """No-op: the Motor client is borrowed from ``MongoDBStore``."""
        return None


async def _iter_gridout(stream) -> "AsyncIterator[bytes]":  # noqa: ANN001
    """Yield a GridFS download stream in 256 KB chunks, closing it after.

    The GridOut is owned here; we close it in ``finally``, guarding for both
    async (Motor's ``GridOut.close`` is a coroutine) and sync/fake-stream
    ``close()`` so a test stub doesn't crash the response.
    """
    try:
        while True:
            chunk = await stream.read(_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result
