"""Content-addressed durable blob store for channel media.

Channel media (Slack/Discord/Mattermost/Teams images, PDFs, videos) is
referenced everywhere by its platform CDN URL, but those URLs rot — Discord
signed URLs expire (~24 h), and Slack/Mattermost/Teams URLs need a live bot
token forever. This store keeps a durable copy of the raw bytes so the
read-through proxy can serve them after the platform URL has died.

The store splits two jobs:

  * The **refs / dedup / url_key / Telegram-guard metadata layer** — always in
    Mongo, regardless of byte backend (this module). The ``channel_media_refs``
    collection, ``normalize_url_key``, the Telegram guard, the sha256
    computation, the size cap, ``has_ref``, and the ref upsert all live here.
  * The **byte storage** — delegated to a pluggable :class:`BlobBackend`
    (GridFS by default, MinIO/S3 when configured). The backend is addressed by
    the neutral ``channels/{channel_id}/{sha256}`` key; it never sees a
    ``url_key``, so the Telegram token-leak guard stays entirely Mongo-side.

Two backing layers:

  * A :class:`BlobBackend` — the raw bytes. Keyed by ``(channel_id, sha256)``
    via :func:`blob_key`; the sha256 is of the RAW bytes (no version salt) and
    blobs are deduped per ``(sha256, channel_id)`` so the same image shared in
    two channels is stored once per channel (channel scoping keeps purge simple).
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
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from beever_atlas.stores.blob_backend import blob_key, blob_prefix
from beever_atlas.stores.gridfs_backend import GridFSBackend

if TYPE_CHECKING:  # pragma: no cover
    from motor.motor_asyncio import (
        AsyncIOMotorCollection,
        AsyncIOMotorDatabase,
    )

    from beever_atlas.stores.blob_backend import BlobBackend, BlobRead
    from beever_atlas.stores.mongodb_store import MongoDBStore

logger = logging.getLogger(__name__)

# Telegram file URLs carry the bot token in the path — never index them.
_TELEGRAM_HOST = "api.telegram.org"


class MediaBlobStore:
    """Durable, content-addressed store for channel media bytes.

    Reuses the shared Motor client from ``MongoDBStore`` (PlatformStore
    pattern) instead of opening its own pool, so the ``channel_media_refs``
    collection lives on the same connection as the rest of the app. The raw
    bytes are delegated to a :class:`BlobBackend` (GridFS by default), while
    the refs / dedup / url_key / Telegram-guard metadata stays here in Mongo
    for every backend.
    """

    def __init__(
        self,
        mongodb: "MongoDBStore | AsyncIOMotorDatabase",
        *,
        backend: "BlobBackend | None" = None,
    ) -> None:
        # Accept either the MongoDBStore (preferred — exposes ``.db``) or a
        # raw database handle so tests can inject a fake db directly.
        db = getattr(mongodb, "db", mongodb)
        self._db: "AsyncIOMotorDatabase" = db
        # Default to GridFS so the OSS/default path and direct-construction
        # callers are unaffected; an injected backend (e.g. MinIO) overrides it.
        self._backend: "BlobBackend" = backend if backend is not None else GridFSBackend(self._db)
        self._refs: "AsyncIOMotorCollection | None" = None

    async def startup(self) -> None:
        """Create the refs indexes and start the byte backend.

        The store owns ONLY the 3 ``channel_media_refs`` indexes; the byte
        backend owns its own bucket/indexes (the two ``channel_media.files``
        metadata indexes moved into ``GridFSBackend``). Index creation is
        idempotent and failure-tolerant: a transient Mongo hiccup at boot must
        not crash the lifespan, mirroring the other stores' ensure-index
        conventions.
        """
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
        except Exception as exc:  # pragma: no cover - defensive boot path
            logger.warning(
                "MediaBlobStore: index creation failed (continuing) error=%s",
                exc,
            )
        await self._backend.startup()

    def _ensure_ready(self) -> tuple["BlobBackend", "AsyncIOMotorCollection"]:
        if self._refs is None:
            raise RuntimeError("MediaBlobStore.startup() was not called")
        return self._backend, self._refs

    async def aclose(self) -> None:
        """Tear down the byte backend (no-op for the shared-client GridFS)."""
        await self._backend.close()

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

        Dedup: if the backend already holds ``(sha256, channel_id)`` the
        upload is skipped. The ref is keyed ``(url_key, channel_id)`` and
        upserted regardless so a re-signed URL refreshes its mapping.
        """
        backend, refs = self._ensure_ready()

        # C2: empty content (an empty/failed download) must persist nothing and
        # index no ref — otherwise a working origin fallback is shadowed by an
        # empty 200. Return the canonical empty-bytes sha so callers keep a
        # stable handle. The ingest path (media_processor._persist_media) has no
        # empty guard, so this store-level guard is the single chokepoint.
        if not content:
            return hashlib.sha256(b"").hexdigest()

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

        # Dedup: skip the byte upload if (sha256, channel_id) already exists.
        # The backend owns the existence probe + the write; GridFS keys it by
        # ``metadata.{sha256,channel_id}`` and MinIO by the object key, both
        # derived from the same ``channels/{channel_id}/{sha256}`` key.
        key = blob_key(channel_id, sha256)
        if not await backend.exists(key):
            await backend.put(
                key,
                content,
                content_type=mime_type,
                filename=filename,
                source_id=source_id,
            )
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

    async def find_refs_for_url(self, url: str, *, channel_id: str | None = None) -> "list[dict]":
        """Return every ref whose ``url_key`` matches ``url``, oldest first.

        The proxy doesn't know which channel a requested URL belongs to, and the
        same ``url_key`` (host+path) can legitimately exist in more than one
        channel (e.g. the same link shared in two channels). Returning ALL
        candidates — deterministically ordered by ``(created_at, channel_id)`` —
        lets the caller authorize each against the principal and serve the first
        one it may access, instead of an arbitrary ``find_one`` that could pick a
        channel the caller can't see (a spurious 403) or a since-purged blob.

        Pass ``channel_id`` to restrict to an exact ``(url_key, channel_id)``
        point lookup when the caller already knows the channel.

        Resilient by contract: an empty/Telegram ``url_key``, a genuine miss, or
        a store error all yield ``[]`` so the read-through proxy can fall through
        to the origin fetch.
        """
        url_key = self.normalize_url_key(url)
        if not url_key:
            return []
        _, refs = self._ensure_ready()
        query = {"url_key": url_key}
        if channel_id is not None:
            query["channel_id"] = channel_id
        try:
            cursor = refs.find(query).sort([("created_at", 1), ("channel_id", 1)])
            return [doc async for doc in cursor]
        except Exception as exc:
            logger.warning("MediaBlobStore: ref lookup failed url_key=%s error=%s", url_key, exc)
            return []

    async def open_ref(self, ref: "dict") -> "BlobRead | None":
        """Open the backend-neutral byte read for a specific ref doc.

        ``None`` when the bytes are gone (e.g. the channel was purged after the
        ref was read), so a caller iterating candidates can skip to the next.
        """
        return await self.open_by_hash(ref["sha256"], ref["channel_id"])

    async def open_by_url(
        self, url: str, *, channel_id: str | None = None
    ) -> "tuple[BlobRead, dict] | None":
        """First openable ``(BlobRead, ref_doc)`` for ``url``, or ``None`` on miss.

        Deterministic (oldest ref first) and purge-safe (skips refs whose blob is
        gone). Channel-agnostic by default; pass ``channel_id`` for an exact
        lookup. NOTE: this does NOT enforce channel access — the read-through
        proxy authorizes per-candidate via :meth:`find_refs_for_url` +
        :meth:`open_ref`; this convenience wrapper is for store-level callers
        (tests, backfill) that already operate within a trusted channel scope.
        """
        for ref in await self.find_refs_for_url(url, channel_id=channel_id):
            read = await self.open_ref(ref)
            if read is not None:
                return read, ref
        return None

    async def open_by_hash(self, sha256: str, channel_id: str) -> "BlobRead | None":
        """Open a backend-neutral byte read for ``(sha256, channel_id)``."""
        backend, _ = self._ensure_ready()
        return await backend.open(blob_key(channel_id, sha256))

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
        partial failure it logs and returns the counts achieved so far. The
        backend purges the bytes for the channel prefix (a GridFS metadata scan
        or an S3 prefix sweep); refs are dropped in one ``delete_many``.
        """
        backend, refs = self._ensure_ready()
        blobs_deleted = 0
        refs_deleted = 0

        try:
            blobs_deleted = await backend.delete_prefix(blob_prefix(channel_id))
        except Exception as exc:
            logger.warning(
                "MediaBlobStore: blob purge failed channel=%s error=%s",
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
        """Return cheap totals for the admin metrics endpoint.

        ``total_refs`` is the Mongo ref count (always Mongo-side);
        ``(total_blobs, total_bytes)`` come from the byte backend.
        """
        backend, refs = self._ensure_ready()
        total_blobs = 0
        total_bytes = 0
        total_refs = 0
        try:
            total_blobs, total_bytes = await backend.stats()
            total_refs = await refs.count_documents({})
        except Exception as exc:
            logger.warning("MediaBlobStore: stats failed error=%s", exc)
        return {
            "total_blobs": total_blobs,
            "total_bytes": total_bytes,
            "total_refs": total_refs,
        }
