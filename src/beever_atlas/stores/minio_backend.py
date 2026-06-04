"""S3-compatible implementation of :class:`BlobBackend` via ``aioboto3``.

The same code targets a local MinIO container (``endpoint_url=http://...:9000``,
path-style addressing) for OSS/EE-on-MinIO and real AWS S3 (``endpoint_url=None``)
for EE-on-AWS — only the construction args differ. Bytes are addressed by the
backend-neutral ``channels/{channel_id}/{sha256}`` key, so a per-channel purge is
a pure ``list_objects_v2`` + ``delete_objects`` prefix sweep.

Lifecycle mirrors ``FileStore``: a long-lived ``aioboto3.Session`` plus a single
startup-opened client held in an ``AsyncExitStack`` and torn down in
:meth:`close`. All reads release their connection deterministically — the
download iterator wraps the streaming body in ``async with`` so the aiohttp
connection is returned to the pool even if the proxy consumer disconnects
mid-stream.
"""

from __future__ import annotations

import logging
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlparse

import aioboto3
from aiobotocore.config import AioConfig
from botocore.exceptions import ClientError

from beever_atlas.stores.blob_backend import BlobRead

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

# Streaming read chunk size — matches the GridFS backend so the read-through
# proxy behaves identically regardless of the byte backend.
_CHUNK_BYTES = 256 * 1024
# delete_objects accepts up to 1000 keys per call; list_objects_v2 returns up
# to 1000 keys per page, so one delete per page is always within bounds.
_PAGE_SIZE = 1000

# Hosts for which plaintext HTTP is acceptable (loopback dev/CI). Anything else
# on ``secure=false`` is a misconfiguration worth a boot-time WARNING.
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _is_not_found(exc: ClientError) -> bool:
    """Return whether an S3 ``ClientError`` is a 404 / missing-object/bucket."""
    error = exc.response.get("Error", {}) if hasattr(exc, "response") else {}
    code = str(error.get("Code", ""))
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return code in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"} or status == 404


class MinioBackend:
    """:class:`BlobBackend` over an S3-compatible store (MinIO or AWS S3)."""

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool = False,
        region: str = "us-east-1",
    ) -> None:
        self._endpoint_url = endpoint_url or None
        self._access_key = access_key
        self._secret_key = secret_key
        self._bucket = bucket
        self._secure = secure
        self._region = region
        self._session = aioboto3.Session()
        self._stack = AsyncExitStack()
        self._client: Any | None = None

    async def startup(self) -> None:
        """Open the long-lived S3 client and ensure the bucket exists."""
        # S5 boot hardening: warn when serving over plaintext HTTP against a
        # non-local endpoint — channel media (potential PII) would transit the
        # wire unencrypted. Loopback dev/CI endpoints are exempt.
        if not self._secure and self._endpoint_url:
            host = (urlparse(self._endpoint_url).hostname or "").lower()
            if host and host not in _LOCAL_HOSTS:
                logger.warning(
                    "MinIO channel-media backend is using plaintext HTTP against a "
                    "non-local endpoint=%s; set CHANNEL_MEDIA_MINIO_SECURE=true",
                    self._endpoint_url,
                )
        # Path-style addressing is required for MinIO (virtual-host style needs
        # wildcard DNS the local endpoint doesn't have); harmless on AWS.
        config = AioConfig(s3={"addressing_style": "path"})
        # aioboto3's ``session.client(...)`` returns an under-typed async context
        # manager; the resulting client is the dynamic botocore S3 surface, so
        # it (and the calls below) are intentionally typed ``Any``.
        client_cm = cast(
            "AbstractAsyncContextManager[Any]",
            self._session.client(
                "s3",
                endpoint_url=self._endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                use_ssl=self._secure,
                config=config,
            ),
        )
        self._client = await self._stack.enter_async_context(client_cm)
        await self._ensure_bucket()

    def _ensure_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("MinioBackend.startup() was not called")
        return self._client

    async def _ensure_bucket(self) -> None:
        """Create the bucket on first boot; tolerate an already-present bucket."""
        client = self._ensure_client()
        try:
            await client.head_bucket(Bucket=self._bucket)
            return
        except ClientError as exc:
            if not _is_not_found(exc):
                raise
        try:
            # us-east-1 must NOT send a LocationConstraint (it's the API default
            # and rejects the constraint); every other region requires it.
            if self._region and self._region != "us-east-1":
                await client.create_bucket(
                    Bucket=self._bucket,
                    CreateBucketConfiguration={"LocationConstraint": self._region},
                )
            else:
                await client.create_bucket(Bucket=self._bucket)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            # Concurrent boots / re-runs: the bucket may now exist and be ours.
            if code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                return
            raise

        # S5: lock the freshly-created bucket down to private-only — block any
        # public ACL / policy. Best-effort: many MinIO builds don't implement
        # the PublicAccessBlock API, so a failure is logged and swallowed (AWS
        # S3 supports it; the channel-media bucket is never meant to be public).
        await self._block_public_access(client)

    async def _block_public_access(self, client: Any) -> None:
        """Best-effort: apply an all-block PublicAccessBlock to the bucket."""
        try:
            await client.put_public_access_block(
                Bucket=self._bucket,
                PublicAccessBlockConfiguration={
                    "BlockPublicAcls": True,
                    "IgnorePublicAcls": True,
                    "BlockPublicPolicy": True,
                    "RestrictPublicBuckets": True,
                },
            )
        except Exception as exc:
            logger.info(
                "MinioBackend: put_public_access_block unsupported/failed bucket=%s error=%s",
                self._bucket,
                exc,
            )

    async def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str,
        filename: str = "blob",  # noqa: ARG002 — ignored (S3 object keyed by key)
        source_id: str = "",  # noqa: ARG002 — ignored
    ) -> None:
        """Store ``data`` under ``key`` with a single PUT.

        ``len(data)`` is always known (the channel-media cap is ≤20 MB and the
        bytes are in memory), so a single ``put_object`` is correct — no
        multipart is needed. ``filename``/``source_id`` are GridFS-only hints
        and intentionally unused here.
        """
        client = self._ensure_client()
        await client.put_object(
            Bucket=self._bucket,
            Key=key,
            Body=data,
            ContentLength=len(data),
            ContentType=content_type,
        )

    async def open(self, key: str) -> BlobRead | None:
        """Open ``key`` for streaming, or ``None`` on a miss."""
        client = self._ensure_client()
        try:
            resp = await client.get_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            logger.warning("MinioBackend: get_object failed key=%s error=%s", key, exc)
            return None
        return BlobRead(
            iterator=_iter_body(resp["Body"]),
            content_type=resp.get("ContentType"),
            size=resp.get("ContentLength"),
        )

    async def exists(self, key: str) -> bool:
        """Return whether ``key`` is already stored (the dedup probe)."""
        client = self._ensure_client()
        try:
            await client.head_object(Bucket=self._bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            logger.warning("MinioBackend: head_object failed key=%s error=%s", key, exc)
            return False
        return True

    async def delete_prefix(self, prefix: str) -> int:
        """Delete every object under ``prefix``; return the count deleted.

        Paginates ``list_objects_v2`` and issues one ``delete_objects`` per page
        (≤1000 keys/page). Never raises — logs and returns the partial count
        (the channel-purge fan-out depends on this).
        """
        client = self._ensure_client()
        deleted = 0
        try:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if not keys:
                    continue
                # ``Quiet=True`` still returns ``Errors`` for keys that failed to
                # delete — count only the successes so a partial purge failure is
                # NOT silently reported as a full success (orphaned PII bytes
                # would otherwise survive while the refs are dropped).
                resp = await client.delete_objects(
                    Bucket=self._bucket, Delete={"Objects": keys, "Quiet": True}
                )
                errors = resp.get("Errors", []) if isinstance(resp, dict) else []
                for err in errors:
                    logger.warning(
                        "MinioBackend: delete_objects failed key=%s code=%s message=%s",
                        err.get("Key"),
                        err.get("Code"),
                        err.get("Message"),
                    )
                deleted += len(keys) - len(errors)
        except ClientError as exc:
            logger.warning(
                "MinioBackend: delete_prefix failed prefix=%s deleted=%d error=%s",
                prefix,
                deleted,
                exc,
            )
        return deleted

    async def stats(self) -> tuple[int, int]:
        """Return ``(total_blobs, total_bytes)`` via a full-bucket list scan.

        A full list is acceptable for the admin metrics endpoint; for very large
        buckets this is O(objects) and should be replaced by bucket metrics.
        """
        client = self._ensure_client()
        total_blobs = 0
        total_bytes = 0
        try:
            paginator = client.get_paginator("list_objects_v2")
            async for page in paginator.paginate(Bucket=self._bucket):
                for obj in page.get("Contents", []):
                    total_blobs += 1
                    total_bytes += int(obj.get("Size", 0) or 0)
        except ClientError as exc:
            logger.warning("MinioBackend: stats failed error=%s", exc)
        return total_blobs, total_bytes

    async def close(self) -> None:
        """Tear down the aiohttp connection pool owned by the S3 client."""
        await self._stack.aclose()
        self._client = None


async def _iter_body(body: Any) -> "AsyncIterator[bytes]":
    """Stream an S3 ``get_object`` body in 256 KB chunks, releasing it after.

    ``body`` is the aiobotocore ``StreamingBody``; using it as an async context
    manager returns the underlying aiohttp connection to the pool on exit even
    if the consumer abandons the iterator mid-stream (the proxy client
    disconnecting), so connections are never leaked. NOTE: ``async with body``
    yields the underlying ``ClientResponse`` (which has no ``iter_chunks``), so
    we iterate ``body`` itself inside the ``with`` — the context manager exists
    purely to guarantee release.
    """
    async with body:
        async for chunk in body.iter_chunks(_CHUNK_BYTES):
            # iter_chunks yields (bytes, end_of_http_chunk) on some botocore
            # versions and raw bytes on others; normalize to bytes.
            data = chunk[0] if isinstance(chunk, tuple) else chunk
            if data:
                yield data
