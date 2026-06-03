"""Backend-neutral byte-storage contract for the channel-media blob store.

``MediaBlobStore`` fuses two jobs: (1) a Mongo refs/dedup/url_key/Telegram
metadata layer and (2) the raw byte storage. This module factors job (2)
behind a thin async ``BlobBackend`` protocol so the bytes can live in GridFS
(default, zero-infra OSS) or an S3-compatible store (MinIO/AWS, EE tier)
while job (1) stays 100% in Mongo for *both* backends.

The load-bearing decision is the **key scheme**: a blob is addressed by
``channels/{channel_id}/{sha256}`` (see :func:`blob_key`). This makes a
per-channel purge a pure prefix operation on S3 (``channels/{id}/``) and a
metadata scan on GridFS, while preserving the existing ``(sha256, channel_id)``
dedup identity — the same image in two channels is stored twice, deliberately,
so the channel purge stays a simple scan.

This module is a pure contract: no I/O, no GridFS/S3 imports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator


@dataclass(frozen=True)
class BlobRead:
    """A backend-neutral handle on an open blob for streaming reads.

    ``iterator`` is an async byte stream that owns its own chunking and
    connection/handle release (the backend closes the GridOut or releases the
    S3 connection in the iterator's ``finally`` / ``async with`` exit). The
    read-through proxy consumes it without knowing which backend produced it.
    """

    iterator: "AsyncIterator[bytes]"
    content_type: str | None
    size: int | None


@runtime_checkable
class BlobBackend(Protocol):
    """The byte-storage half of ``MediaBlobStore``.

    Implementations own only the bytes — never the ``channel_media_refs``
    collection, ``normalize_url_key``, the Telegram guard, sha256 computation,
    the size cap, or ``has_ref`` (all of which stay in Mongo regardless of
    backend). Resilience contract mirrors the store: ``open`` returns ``None``
    on a miss, ``exists`` returns ``False`` on a miss, ``delete_prefix`` and
    ``stats`` swallow per-item failures and return partial counts.
    """

    async def startup(self) -> None:
        """Bind the backing bucket/client and ensure its indexes exist."""
        ...

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        filename: str = "blob",
        source_id: str = "",
    ) -> None:
        """Store ``data`` under ``key`` (no-op if the key already exists).

        ``filename``/``source_id`` are descriptive hints the GridFS backend
        stamps onto its file doc to preserve the legacy on-disk shape; the
        MinIO backend ignores them (an S3 object is keyed purely by ``key``).
        """
        ...

    async def open(self, key: str) -> BlobRead | None:
        """Open ``key`` for streaming, or ``None`` on a miss."""
        ...

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` is already stored (the dedup probe)."""
        ...

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every blob under ``prefix``; return the count deleted."""
        ...

    async def stats(self) -> tuple[int, int]:
        """Return ``(total_blobs, total_bytes)`` across the whole backend."""
        ...

    async def close(self) -> None:
        """Tear down any backend-owned connection pool (no-op if shared)."""
        ...


def blob_key(channel_id: str, sha256: str) -> str:
    """Return the storage key for a blob: ``channels/{channel_id}/{sha256}``.

    Channel-scoped + content-addressed: the same content in two channels gets
    two keys (two stored copies, deliberately) so a channel purge is a clean
    prefix delete and the existing ``(sha256, channel_id)`` dedup identity is
    preserved.
    """
    return f"channels/{channel_id}/{sha256}"


def blob_prefix(channel_id: str) -> str:
    """Return the purge prefix for a channel: ``channels/{channel_id}/``.

    The trailing slash is load-bearing: without it, prefix ``channels/1``
    would also match ``channels/12``'s objects on an S3 ``list_objects_v2``.
    """
    return f"channels/{channel_id}/"
