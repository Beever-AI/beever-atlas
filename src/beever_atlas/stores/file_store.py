"""Durable file-blob store for Ask attachments (Mongo GridFS).

Stores the raw bytes of user-uploaded attachments so the Ask UI can offer
image previews and document downloads on the same `file_id` the upload
endpoint mints. Extracted text continues to live in the Ask prompt and
chat-history message subdoc — this store is purely for the blob.

Retention: no TTL yet. Blobs are keyed by a server-generated `file_id`
(UUID4 hex) and stamped with `owner_user_id` so the GET endpoint can
fail-closed on cross-tenant access.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING

from motor.motor_asyncio import (
    AsyncIOMotorClient,
    AsyncIOMotorGridFSBucket,
)

if TYPE_CHECKING:  # pragma: no cover
    from motor.motor_asyncio import AsyncIOMotorGridOut

logger = logging.getLogger(__name__)

# Bucket name keeps the chunks + files collections grouped so a future
# TTL index / purge job can target them cleanly.
_BUCKET = "ask_files"


class FileStore:
    """Thin async wrapper over a Mongo GridFS bucket for attachment blobs."""

    def __init__(self, mongodb_uri: str, *, db_name: str = "beever_atlas") -> None:
        self._uri = mongodb_uri
        self._db_name = db_name
        self._client: AsyncIOMotorClient | None = None
        self._bucket: AsyncIOMotorGridFSBucket | None = None

    async def startup(self) -> None:
        self._client = AsyncIOMotorClient(self._uri)
        self._bucket = AsyncIOMotorGridFSBucket(self._client[self._db_name], bucket_name=_BUCKET)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None
            self._bucket = None

    def _ensure_ready(self) -> AsyncIOMotorGridFSBucket:
        if self._bucket is None:
            raise RuntimeError("FileStore.startup() was not called")
        return self._bucket

    async def save(
        self,
        *,
        content: bytes,
        filename: str,
        mime_type: str,
        owner_user_id: str,
    ) -> str:
        """Persist bytes and return a stable `file_id` (UUID hex).

        `file_id` is deliberately decoupled from Mongo's `_id` so the
        upload handler can mint it up front and hand the same value to
        GridFS as metadata — that way the GET endpoint only needs the
        string the client has, never GridFS's ObjectId.
        """
        bucket = self._ensure_ready()
        file_id = uuid.uuid4().hex
        metadata = {
            "file_id": file_id,
            "owner_user_id": owner_user_id,
            "mime_type": mime_type,
        }
        # `open_upload_stream_with_id` gives us an explicit write handle so
        # we never hand GridFS a fake file-object. Using our own `file_id`
        # as the GridFS `_id` means GETs look the blob up directly by id
        # (no extra `files.find({"metadata.file_id": ...})` round-trip).
        stream = bucket.open_upload_stream_with_id(file_id, filename, metadata=metadata)
        try:
            await stream.write(content)
        finally:
            await stream.close()
        logger.info(
            "file_store: saved file_id=%s owner=%s size=%d mime=%s",
            file_id,
            owner_user_id,
            len(content),
            mime_type,
        )
        return file_id

    async def open(self, file_id: str) -> "AsyncIOMotorGridOut | None":
        """Open a GridOut cursor for the given id, or None if missing."""
        bucket = self._ensure_ready()
        try:
            return await bucket.open_download_stream(file_id)
        except Exception:
            # NoFile raises a concrete gridfs.errors.NoFile; catching broadly
            # so motor/pymongo version drift doesn't leak into callers.
            return None
