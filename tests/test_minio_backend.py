"""Unit tests for :class:`MinioBackend` over a hand-stubbed async S3 client.

These run with NO live infra: a small in-memory fake reproduces the slice of
the S3 API ``MinioBackend`` touches (``head_bucket``/``create_bucket``,
``put_object``/``get_object``/``head_object``, ``get_paginator``/
``list_objects_v2``, ``delete_objects``) including the streaming-body context
manager + ``iter_chunks`` and ``ClientError`` 404 semantics. The fake is
injected onto a real ``MinioBackend`` (its ``startup`` is driven through the
fake so ``ensure_bucket`` is exercised), so we test the backend's real
``put``/``open``/``exists``/``delete_prefix``/``stats`` logic — only the wire
is stubbed.

Covers the roundtrip, miss→None, the trailing-slash prefix isolation
(``channels/C1`` must not match ``channels/C12``), stats, ensure_bucket
idempotency, and deterministic connection release on a partially-consumed read.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import logging
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError

from beever_atlas.stores.blob_backend import blob_key, blob_prefix
from beever_atlas.stores.minio_backend import MinioBackend

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncGenerator, AsyncIterator


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the Settings cache around every test in this module.

    ``get_settings()`` is an ``lru_cache`` singleton shared process-wide; this
    module is collected early, so priming the cache from the raw environment
    before conftest's ``_auth_bypass`` exports ``BEEVER_API_KEYS=test-key``
    would 401 every later auth-dependent test in a combined run. Clearing on
    setup (module autouse runs after conftest autouse) and teardown keeps this
    module from depending on, or corrupting, the shared cache.
    """
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# In-memory async S3 fake
# ---------------------------------------------------------------------------


def _client_error(code: str, status: int, op: str) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        op,
    )


class _FakeBody:
    """Async streaming body with the ``iter_chunks`` + context-manager contract."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self.released = False

    async def __aenter__(self) -> _FakeBody:
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        self.released = True
        return False

    async def iter_chunks(self, size: int) -> "AsyncIterator[bytes]":
        for i in range(0, len(self._data), size):
            yield self._data[i : i + size]


class _FakePaginator:
    def __init__(self, store: dict[str, tuple[bytes, str]]) -> None:
        self._store = store

    async def paginate(self, *, Bucket: str, Prefix: str = "") -> "AsyncIterator[dict[str, Any]]":  # noqa: N803
        contents = [
            {"Key": k, "Size": len(v[0])}
            for k, v in sorted(self._store.items())
            if k.startswith(Prefix)
        ]
        # Page in fixed slices to exercise the multi-page delete/stats loops.
        page_size = 2
        if not contents:
            yield {}
            return
        for i in range(0, len(contents), page_size):
            yield {"Contents": contents[i : i + page_size]}


class _FakeS3Client:
    """Minimal in-memory S3 reproducing the ops ``MinioBackend`` calls."""

    def __init__(self) -> None:
        self.buckets: set[str] = set()
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.last_bodies: list[_FakeBody] = []
        # Keys for which delete_objects should report an Errors entry (and
        # NOT actually delete) — drives the C1 partial-failure test.
        self.delete_error_keys: set[str] = set()
        # When set, put_public_access_block raises this (drives S5 swallow).
        self.pab_error: Exception | None = None
        self.pab_calls: list[str] = []

    async def head_bucket(self, *, Bucket: str) -> dict[str, Any]:  # noqa: N803
        if Bucket not in self.buckets:
            raise _client_error("404", 404, "HeadBucket")
        return {}

    async def create_bucket(self, *, Bucket: str, **_kw: Any) -> dict[str, Any]:  # noqa: N803
        if Bucket in self.buckets:
            raise _client_error("BucketAlreadyOwnedByYou", 409, "CreateBucket")
        self.buckets.add(Bucket)
        return {}

    async def put_object(  # noqa: N803
        self, *, Bucket: str, Key: str, Body: bytes, ContentLength: int, ContentType: str
    ) -> dict[str, Any]:
        self.objects[Key] = (Body, ContentType)
        return {}

    async def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise _client_error("NoSuchKey", 404, "GetObject")
        data, ctype = self.objects[Key]
        body = _FakeBody(data)
        self.last_bodies.append(body)
        return {"Body": body, "ContentType": ctype, "ContentLength": len(data)}

    async def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise _client_error("404", 404, "HeadObject")
        return {}

    async def delete_objects(self, *, Bucket: str, Delete: dict[str, Any]) -> dict[str, Any]:  # noqa: N803
        errors: list[dict[str, str]] = []
        for obj in Delete.get("Objects", []):
            key = obj["Key"]
            if key in self.delete_error_keys:
                # Report the failure and DON'T delete (S3 partial-failure shape).
                errors.append({"Key": key, "Code": "AccessDenied", "Message": "nope"})
                continue
            self.objects.pop(key, None)
        return {"Errors": errors} if errors else {}

    async def put_public_access_block(  # noqa: N803
        self, *, Bucket: str, PublicAccessBlockConfiguration: dict[str, Any]
    ) -> dict[str, Any]:
        self.pab_calls.append(Bucket)
        if self.pab_error is not None:
            raise self.pab_error
        return {}

    def get_paginator(self, _name: str) -> _FakePaginator:
        return _FakePaginator(self.objects)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _make_backend(*, bucket_exists: bool = False) -> tuple[MinioBackend, _FakeS3Client]:
    """Build a MinioBackend whose client is the in-memory fake (driven startup)."""
    backend = MinioBackend(
        endpoint_url="http://localhost:9000",
        access_key="key",
        secret_key="secret",
        bucket="atlas-media-test",
        secure=False,
        region="us-east-1",
    )
    fake = _FakeS3Client()
    if bucket_exists:
        fake.buckets.add("atlas-media-test")
    # Bypass aioboto3 entirely: inject the fake client + a real (empty) stack.
    backend._client = fake
    backend._stack = AsyncExitStack()
    await backend._ensure_bucket()  # exercises head/create idempotency
    return backend, fake


async def _drain(it: "AsyncIterator[bytes]") -> bytes:
    buf = b""
    async for chunk in it:
        buf += chunk
    return buf


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestEnsureBucket:
    async def test_startup_creates_bucket_idempotent(self):
        backend, fake = await _make_backend(bucket_exists=False)
        assert "atlas-media-test" in fake.buckets
        # A second ensure with the bucket present is a no-op (head_bucket hit).
        await backend._ensure_bucket()
        assert fake.buckets == {"atlas-media-test"}

    async def test_ensure_bucket_tolerates_already_owned(self):
        # Bucket already present from a prior boot — head_bucket succeeds, no
        # create_bucket attempted; still a clean no-op.
        backend, fake = await _make_backend(bucket_exists=True)
        await backend._ensure_bucket()
        assert "atlas-media-test" in fake.buckets


class TestRoundtrip:
    async def test_put_then_exists_then_open_roundtrip(self):
        backend, _fake = await _make_backend()
        key = blob_key("C1", "sha-aaa")
        await backend.put(key, b"the-bytes", content_type="image/png")

        assert await backend.exists(key) is True
        read = await backend.open(key)
        assert read is not None
        assert read.content_type == "image/png"
        assert read.size == len(b"the-bytes")
        assert await _drain(read.iterator) == b"the-bytes"

    async def test_open_miss_returns_none(self):
        backend, _fake = await _make_backend()
        assert await backend.open(blob_key("C1", "missing")) is None

    async def test_exists_false_on_missing(self):
        backend, _fake = await _make_backend()
        assert await backend.exists(blob_key("C1", "missing")) is False

    async def test_open_iterator_releases_connection(self):
        backend, fake = await _make_backend()
        key = blob_key("C1", "sha-rel")
        await backend.put(key, b"x" * (300 * 1024), content_type="application/octet-stream")

        # Fully consume → body released.
        read = await backend.open(key)
        assert read is not None
        await _drain(read.iterator)
        assert fake.last_bodies[-1].released is True

        # Partial-then-abandon → still released via the iterator's async-with.
        read2 = await backend.open(key)
        assert read2 is not None
        agen = cast("AsyncGenerator[bytes, None]", read2.iterator)
        await agen.__anext__()  # pull one chunk only
        await agen.aclose()  # consumer disconnects mid-stream
        assert fake.last_bodies[-1].released is True


class TestPrefixIsolation:
    async def test_delete_prefix_removes_only_that_channel(self):
        backend, _fake = await _make_backend()
        # channels/C1/ and channels/C12/ — the trailing slash must keep them
        # apart (channels/C1 would otherwise prefix-match channels/C12).
        await backend.put(blob_key("C1", "a"), b"a1", content_type="image/png")
        await backend.put(blob_key("C1", "b"), b"b1", content_type="image/png")
        await backend.put(blob_key("C12", "c"), b"c1", content_type="image/png")

        deleted = await backend.delete_prefix(blob_prefix("C1"))
        assert deleted == 2
        assert await backend.exists(blob_key("C1", "a")) is False
        assert await backend.exists(blob_key("C1", "b")) is False
        # C12 untouched.
        assert await backend.exists(blob_key("C12", "c")) is True

    async def test_delete_prefix_empty_channel_returns_zero(self):
        backend, _fake = await _make_backend()
        assert await backend.delete_prefix(blob_prefix("nope")) == 0


class TestStats:
    async def test_stats_counts_and_bytes(self):
        backend, _fake = await _make_backend()
        await backend.put(blob_key("C1", "a"), b"aaaa", content_type="image/png")
        await backend.put(blob_key("C2", "b"), b"bb", content_type="image/png")
        total_blobs, total_bytes = await backend.stats()
        assert total_blobs == 2
        assert total_bytes == len(b"aaaa") + len(b"bb")

    async def test_stats_empty_bucket(self):
        backend, _fake = await _make_backend()
        assert await backend.stats() == (0, 0)


class TestDeletePrefixPartialFailure:
    async def test_partial_delete_failure_excluded_from_count(self):
        """C1: delete_objects reporting Errors for one key → count excludes it,
        the failed key survives, and the never-raises contract holds."""
        backend, fake = await _make_backend()
        await backend.put(blob_key("C1", "a"), b"a1", content_type="image/png")
        await backend.put(blob_key("C1", "b"), b"b1", content_type="image/png")
        await backend.put(blob_key("C1", "c"), b"c1", content_type="image/png")
        # Fail the delete of one specific object.
        fail_key = blob_key("C1", "b")
        fake.delete_error_keys = {fail_key}

        deleted = await backend.delete_prefix(blob_prefix("C1"))

        # 3 listed, 1 failed → 2 counted (NOT 3 — the old bug counted blind).
        assert deleted == 2
        # The failed key is still present (not silently reported as deleted).
        assert await backend.exists(fail_key) is True
        assert await backend.exists(blob_key("C1", "a")) is False
        assert await backend.exists(blob_key("C1", "c")) is False

    async def test_delete_prefix_never_raises_on_partial_failure(self):
        backend, fake = await _make_backend()
        await backend.put(blob_key("C1", "a"), b"a1", content_type="image/png")
        fake.delete_error_keys = {blob_key("C1", "a")}
        # Must not raise even though every delete in the page errored.
        deleted = await backend.delete_prefix(blob_prefix("C1"))
        assert deleted == 0


class TestPublicAccessBlock:
    async def test_block_public_access_applied_on_create(self):
        """S5: a freshly-created bucket gets a PublicAccessBlock."""
        backend, fake = await _make_backend(bucket_exists=False)
        assert fake.pab_calls == ["atlas-media-test"]

    async def test_block_public_access_unsupported_is_swallowed(self):
        """S5: a store that doesn't support the API must not crash startup."""
        backend = MinioBackend(
            endpoint_url="http://localhost:9000",
            access_key="key",
            secret_key="secret",
            bucket="atlas-media-test",
            secure=False,
            region="us-east-1",
        )
        fake = _FakeS3Client()
        fake.pab_error = _client_error("NotImplemented", 501, "PutPublicAccessBlock")
        backend._client = fake
        backend._stack = AsyncExitStack()
        # Must not raise — the unsupported API is best-effort.
        await backend._ensure_bucket()
        assert "atlas-media-test" in fake.buckets

    async def test_block_public_access_not_applied_when_bucket_exists(self):
        """S5: an already-present bucket is NOT re-locked (create branch only)."""
        backend, fake = await _make_backend(bucket_exists=True)
        assert fake.pab_calls == []


class TestInsecureBootWarning:
    """Capture the boot WARNING by attaching a handler directly to the module
    logger — the app's JSON logging config doesn't propagate to the root, so
    ``caplog``'s root-handler capture sees nothing."""

    @staticmethod
    async def _startup_capture(endpoint: str) -> list[str]:
        backend = MinioBackend(
            endpoint_url=endpoint,
            access_key="key",
            secret_key="secret",
            bucket="atlas-media-test",
            secure=False,
            region="us-east-1",
        )
        fake = _FakeS3Client()
        backend._session = cast(Any, _FakeSession(fake))

        records: list[str] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        from beever_atlas.stores import minio_backend as mb_mod

        handler = _Capture(level=logging.WARNING)
        mb_mod.logger.addHandler(handler)
        try:
            await backend.startup()
        finally:
            mb_mod.logger.removeHandler(handler)
        return records

    async def test_warns_on_plaintext_nonlocal_endpoint(self):
        records = await self._startup_capture("http://minio.internal.example:9000")
        assert any("plaintext HTTP" in msg for msg in records)

    async def test_no_warning_for_localhost(self):
        records = await self._startup_capture("http://localhost:9000")
        assert not any("plaintext HTTP" in msg for msg in records)


class _FakeSession:
    """Minimal ``aioboto3.Session`` stub whose ``client(...)`` yields the fake.

    Lets ``MinioBackend.startup()`` run end-to-end (exercising the boot
    WARNING + ensure_bucket) without aioboto3 / a live endpoint.
    """

    def __init__(self, fake: _FakeS3Client) -> None:
        self._fake = fake

    def client(self, *_a: Any, **_kw: Any) -> "_FakeClientCM":
        return _FakeClientCM(self._fake)


class _FakeClientCM:
    def __init__(self, fake: _FakeS3Client) -> None:
        self._fake = fake

    async def __aenter__(self) -> _FakeS3Client:
        return self._fake

    async def __aexit__(self, *_exc: object) -> bool:
        return False
