"""Unit + integration tests for the durable channel-media blob store.

Unit tests mock the Motor surface (``MagicMock`` collections + ``AsyncMock``
methods, no real Mongo) in the style of ``tests/test_media_cache.py``. They
cover URL normalization, content-addressed dedup, ref upsert / skip, the
resilient read-through ``open_by_url``, channel purge, and ``has_ref``.

The ``TestMediaBlobStoreIntegration`` class drives a live ``mongo:7`` (the CI
service) the same way the repo's other live-backend tests gate — a
module-level ``pytest.mark.skipif`` that pings ``settings.mongodb_uri`` once
and skips when no mongod answers (mirrors the conftest
``MongoClient(..., serverSelectionTimeoutMS=1000)`` probe and the graph
contract's ``skipif`` precedent).

Convention: no ``@pytest.mark.asyncio`` decorators on the unit class; pyproject
sets ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.stores.gridfs_backend import GridFSBackend
from beever_atlas.stores.media_blob_store import MediaBlobStore


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the Settings cache around every test in this module.

    ``get_settings()`` is an ``lru_cache`` singleton shared process-wide.
    These tests run early in alphabetical collection order and (mostly) do
    not request ``monkeypatch``, so conftest's ``_init_stores_for_tests``
    can prime the cache before ``_auth_bypass`` exports the test env vars —
    breaking every later auth-dependent test with 401s. Clearing on setup
    (module autouse runs after conftest autouse) and again on teardown keeps
    this module from depending on, or corrupting, the shared cache.
    """
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

CHANNEL = "C123"
SOURCE = "slack:T1"
MESSAGE = "M456"
SAMPLE = b"fake-image-bytes"
SAMPLE_SHA = hashlib.sha256(SAMPLE).hexdigest()
MIME = "image/png"
FILENAME = "shot.png"

DISCORD_URL = "https://cdn.discordapp.com/attachments/1/2/shot.png?ex=abc&is=def&hm=0011"
DISCORD_URL_RESIGNED = "https://cdn.discordapp.com/attachments/1/2/shot.png?ex=999&is=888&hm=ffff"
TELEGRAM_URL = "https://api.telegram.org/file/bot12345:TOKEN/photos/a.jpg"
DISCORD_URL_KEY = "cdn.discordapp.com/attachments/1/2/shot.png"


class _FakeUploadStream:
    """Captures bytes written through the GridFS upload-stream API."""

    def __init__(self) -> None:
        self.written = b""

    async def write(self, data: bytes) -> None:
        self.written += data

    async def close(self) -> None:
        return None


class _AsyncCursor:
    """Chainable async cursor stub: ``find(q).sort(...)`` then ``async for``."""

    def __init__(self, docs: "list[dict]", *, raises: bool = False) -> None:
        self._docs = list(docs)
        self._raises = raises

    def sort(self, *_a, **_kw) -> "_AsyncCursor":
        return self

    async def __aiter__(self):
        if self._raises:
            raise RuntimeError("mongo down")
        for doc in self._docs:
            yield doc


def _make_store(
    *,
    files_existing: dict | None = None,
    ref_existing: dict | None = None,
    files_find_one_raises: bool = False,
    ref_find_one_raises: bool = False,
) -> tuple[MediaBlobStore, MagicMock, MagicMock, MagicMock, _FakeUploadStream]:
    """Build a MediaBlobStore over fully-mocked Motor objects.

    Returns (store, files_col, refs_col, bucket, upload_stream) so assertions
    can inspect what the store did.
    """
    upload_stream = _FakeUploadStream()

    files_col = MagicMock(name="files_col")
    if files_find_one_raises:
        files_col.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    else:
        files_col.find_one = AsyncMock(return_value=files_existing)
    files_col.count_documents = AsyncMock(return_value=0)

    refs_col = MagicMock(name="refs_col")
    if ref_find_one_raises:
        refs_col.find_one = AsyncMock(side_effect=RuntimeError("mongo down"))
    else:
        refs_col.find_one = AsyncMock(return_value=ref_existing)
    # ``find(query).sort(...)`` async cursor (the read path). Yields the single
    # configured ref (or nothing on a miss); raises mid-iteration to exercise
    # the resilience path.
    refs_col.find = MagicMock(
        side_effect=lambda *_a, **_kw: _AsyncCursor(
            [ref_existing] if ref_existing is not None else [],
            raises=ref_find_one_raises,
        )
    )
    refs_col.update_one = AsyncMock(return_value=None)
    refs_col.delete_many = AsyncMock(return_value=MagicMock(deleted_count=3))
    refs_col.count_documents = AsyncMock(return_value=0)
    refs_col.create_index = AsyncMock(return_value="idx")

    db: dict[str, Any] = {
        "channel_media.files": files_col,
        "channel_media_refs": refs_col,
    }

    bucket = MagicMock(name="bucket")
    bucket.open_upload_stream = MagicMock(return_value=upload_stream)
    bucket.open_download_stream = AsyncMock(return_value=MagicMock(name="gridout"))
    bucket.delete = AsyncMock(return_value=None)

    # After the backend split, the bytes live behind a GridFSBackend. Wire one
    # over the SAME fake db + bucket so the legacy assertions (upload-stream
    # writes, dedup skip, download-stream id, per-channel delete loop) hold —
    # GridFSBackend reproduces the exact metadata.{sha256,channel_id} queries
    # against ``channel_media.files`` (the ``db`` dict's files_col).
    backend = GridFSBackend(db)  # type: ignore[arg-type]  # fake db is a dict
    backend._bucket = bucket
    store = MediaBlobStore(MagicMock(db=db), backend=backend)
    store._refs = refs_col
    return store, files_col, refs_col, bucket, upload_stream


# ---------------------------------------------------------------------------
# normalize_url_key
# ---------------------------------------------------------------------------


class TestNormalizeUrlKey:
    def test_strips_scheme_query_fragment(self):
        assert (
            MediaBlobStore.normalize_url_key("https://Cdn.Example.com/a/b.png?token=secret#frag")
            == "cdn.example.com/a/b.png"
        )

    def test_lowercases_host_only(self):
        # Host lowercased; path case preserved.
        assert (
            MediaBlobStore.normalize_url_key("https://HOST.com/Path/File.PNG")
            == "host.com/Path/File.PNG"
        )

    def test_discord_signed_and_resigned_share_one_key(self):
        assert MediaBlobStore.normalize_url_key(DISCORD_URL) == MediaBlobStore.normalize_url_key(
            DISCORD_URL_RESIGNED
        )
        assert MediaBlobStore.normalize_url_key(DISCORD_URL) == DISCORD_URL_KEY

    def test_telegram_host_yields_empty(self):
        assert MediaBlobStore.normalize_url_key(TELEGRAM_URL) == ""

    def test_slack_stable_across_token_rotation(self):
        url = "https://files.slack.com/files-pri/T1-F1/shot.png?t=AAA"
        resigned = "https://files.slack.com/files-pri/T1-F1/shot.png?t=BBB"
        key = MediaBlobStore.normalize_url_key(url)
        assert key == "files.slack.com/files-pri/T1-F1/shot.png"
        assert MediaBlobStore.normalize_url_key(resigned) == key

    def test_teams_graph_stable_across_tempauth_rotation(self):
        url = "https://graph.microsoft.com/v1.0/drives/D1/items/I1/content?tempauth=AAA"
        resigned = "https://graph.microsoft.com/v1.0/drives/D1/items/I1/content?tempauth=ZZZ"
        key = MediaBlobStore.normalize_url_key(url)
        assert key == "graph.microsoft.com/v1.0/drives/D1/items/I1/content"
        assert MediaBlobStore.normalize_url_key(resigned) == key

    def test_mattermost_extensionless_identity_is_host_path(self):
        url = "https://team.example.com/api/v4/files/fileid123"
        assert MediaBlobStore.normalize_url_key(url) == "team.example.com/api/v4/files/fileid123"

    def test_empty_and_invalid_yield_empty(self):
        assert MediaBlobStore.normalize_url_key("") == ""
        assert MediaBlobStore.normalize_url_key("not a url") == ""
        assert MediaBlobStore.normalize_url_key("mailto:x@y.com") == ""


# ---------------------------------------------------------------------------
# save_blob
# ---------------------------------------------------------------------------


class TestSaveBlob:
    async def _save(self, store, *, content=SAMPLE, url=DISCORD_URL):
        return await store.save_blob(
            content=content,
            mime_type=MIME,
            filename=FILENAME,
            channel_id=CHANNEL,
            source_id=SOURCE,
            message_id=MESSAGE,
            platform_url=url,
        )

    async def test_computes_raw_sha256_no_version_salt(self):
        store, _files, _refs, _bucket, _stream = _make_store()
        sha = await self._save(store)
        assert sha == SAMPLE_SHA  # raw sha256(content), no MEDIA_CACHE_VERSION mix

    async def test_uploads_and_upserts_ref_on_first_store(self):
        store, _files, refs, _bucket, stream = _make_store(files_existing=None)
        await self._save(store)
        assert stream.written == SAMPLE
        refs.update_one.assert_awaited_once()
        flt = refs.update_one.call_args[0][0]
        assert flt == {"url_key": DISCORD_URL_KEY, "channel_id": CHANNEL}

    async def test_dedups_on_existing_sha_channel(self):
        # GridFS already has (sha256, channel_id) → skip the upload, still upsert ref.
        store, _files, refs, bucket, stream = _make_store(files_existing={"_id": "existing"})
        await self._save(store)
        bucket.open_upload_stream.assert_not_called()
        assert stream.written == b""
        refs.update_one.assert_awaited_once()

    async def test_skips_ref_for_empty_url_key_but_stores_blob(self):
        store, _files, refs, bucket, stream = _make_store(files_existing=None)
        sha = await self._save(store, url=TELEGRAM_URL)
        assert sha == SAMPLE_SHA
        assert stream.written == SAMPLE  # blob still stored
        refs.update_one.assert_not_awaited()  # ref skipped for Telegram

    async def test_rejects_oversized_content(self, monkeypatch):
        from beever_atlas.infra import config as config_mod

        settings = config_mod.get_settings()
        monkeypatch.setattr(settings, "media_max_file_size_mb", 0, raising=False)

        store, _files, refs, bucket, stream = _make_store(files_existing=None)
        big = b"x" * 1024  # > 0 MB
        sha = await self._save(store, content=big)

        assert sha == hashlib.sha256(big).hexdigest()  # sha still returned
        bucket.open_upload_stream.assert_not_called()  # not stored
        refs.update_one.assert_not_awaited()

    async def test_empty_content_persists_nothing(self):
        """C2: empty bytes → canonical empty sha, no byte write, no ref upsert."""
        store, files, refs, bucket, stream = _make_store(files_existing=None)
        sha = await self._save(store, content=b"")

        assert sha == hashlib.sha256(b"").hexdigest()
        bucket.open_upload_stream.assert_not_called()  # nothing stored
        assert stream.written == b""
        refs.update_one.assert_not_awaited()  # no ref indexed
        # The empty guard returns BEFORE the dedup probe — no Mongo touch at all.
        files.find_one.assert_not_awaited()

    async def test_gridfs_single_probe_on_fresh_key(self):
        """C3: a fresh save dedup-probes ONCE (the store's exists()) then writes.

        The GridFSBackend.put no longer re-probes, so ``channel_media.files``'s
        ``find_one`` (the only probe surface) is awaited exactly once.
        """
        store, files, _refs, _bucket, stream = _make_store(files_existing=None)
        await self._save(store)
        assert stream.written == SAMPLE
        assert files.find_one.await_count == 1, "exactly one dedup probe (no second exists())"


# ---------------------------------------------------------------------------
# open_by_url
# ---------------------------------------------------------------------------


class TestOpenByUrl:
    async def test_hit_returns_stream_and_ref(self):
        ref = {"url_key": DISCORD_URL_KEY, "channel_id": CHANNEL, "sha256": SAMPLE_SHA}
        store, files, _refs, bucket, _stream = _make_store(
            ref_existing=ref, files_existing={"_id": "fid"}
        )
        result = await store.open_by_url(DISCORD_URL)
        assert result is not None
        stream, got_ref = result
        assert got_ref == ref
        bucket.open_download_stream.assert_awaited_once_with("fid")

    async def test_resigned_url_hits_same_ref(self):
        ref = {"url_key": DISCORD_URL_KEY, "channel_id": CHANNEL, "sha256": SAMPLE_SHA}
        store, _files, refs, _bucket, _stream = _make_store(
            ref_existing=ref, files_existing={"_id": "fid"}
        )
        await store.open_by_url(DISCORD_URL_RESIGNED)
        # The expired/re-signed URL still queried by the stable host+path key.
        assert refs.find.call_args[0][0] == {"url_key": DISCORD_URL_KEY}

    async def test_miss_returns_none(self):
        store, _files, _refs, _bucket, _stream = _make_store(ref_existing=None)
        assert await store.open_by_url(DISCORD_URL) is None

    async def test_empty_url_key_returns_none_without_query(self):
        store, _files, refs, _bucket, _stream = _make_store()
        assert await store.open_by_url(TELEGRAM_URL) is None
        refs.find_one.assert_not_awaited()

    async def test_store_error_returns_none_resilient(self):
        store, _files, _refs, _bucket, _stream = _make_store(ref_find_one_raises=True)
        assert await store.open_by_url(DISCORD_URL) is None


# ---------------------------------------------------------------------------
# has_ref
# ---------------------------------------------------------------------------


class TestHasRef:
    async def test_true_when_ref_present(self):
        store, _files, _refs, _bucket, _stream = _make_store(ref_existing={"_id": "x"})
        assert await store.has_ref(DISCORD_URL, CHANNEL) is True

    async def test_false_when_absent(self):
        store, _files, _refs, _bucket, _stream = _make_store(ref_existing=None)
        assert await store.has_ref(DISCORD_URL, CHANNEL) is False

    async def test_false_for_empty_url_key(self):
        store, _files, refs, _bucket, _stream = _make_store()
        assert await store.has_ref(TELEGRAM_URL, CHANNEL) is False
        refs.find_one.assert_not_awaited()


# ---------------------------------------------------------------------------
# startup — index creation
# ---------------------------------------------------------------------------


class TestStartup:
    async def test_creates_gridfs_files_indexes(self, monkeypatch):
        """startup() must index ``channel_media.files`` metadata, not just refs.

        The dedup probe (save_blob), content-hash read (open_by_hash), and
        purge scan (delete_by_channel) all query the GridFS files collection by
        ``metadata.{sha256,channel_id}``; GridFS never auto-indexes those, so
        the default GridFSBackend's startup has to create them or every probe
        is a collection scan. After the backend split these two indexes are
        owned by ``GridFSBackend.startup`` (the 3 refs indexes stay on the
        store), but ``MediaBlobStore.startup`` drives the backend, so the
        end-to-end ``store.startup()`` must still create them.
        """
        refs_col = MagicMock(name="refs_col")
        refs_col.create_index = AsyncMock(return_value="idx")
        files_col = MagicMock(name="files_col")
        files_col.create_index = AsyncMock(return_value="idx")

        db: dict[str, Any] = {
            "channel_media_refs": refs_col,
            "channel_media.files": files_col,
        }
        # Avoid constructing a real GridFS bucket against the fake db — the
        # bucket is now built inside GridFSBackend.startup.
        monkeypatch.setattr(
            "beever_atlas.stores.gridfs_backend.AsyncIOMotorGridFSBucket",
            lambda *_a, **_k: MagicMock(name="bucket"),
        )

        store = MediaBlobStore(MagicMock(db=db))
        await store.startup()

        files_index_calls = [c.args for c in files_col.create_index.await_args_list]
        assert ([("metadata.sha256", 1), ("metadata.channel_id", 1)],) in files_index_calls
        assert ("metadata.channel_id",) in files_index_calls

    async def test_index_failure_is_swallowed(self, monkeypatch):
        # A transient Mongo hiccup at boot must not crash the lifespan.
        refs_col = MagicMock(name="refs_col")
        refs_col.create_index = AsyncMock(side_effect=RuntimeError("mongo down"))
        files_col = MagicMock(name="files_col")
        files_col.create_index = AsyncMock(side_effect=RuntimeError("mongo down"))
        db: dict[str, Any] = {
            "channel_media_refs": refs_col,
            "channel_media.files": files_col,
        }
        monkeypatch.setattr(
            "beever_atlas.stores.gridfs_backend.AsyncIOMotorGridFSBucket",
            lambda *_a, **_k: MagicMock(name="bucket"),
        )
        store = MediaBlobStore(MagicMock(db=db))
        await store.startup()  # must not raise (both refs + backend swallow)


# ---------------------------------------------------------------------------
# delete_by_channel
# ---------------------------------------------------------------------------


class TestDeleteByChannel:
    async def test_deletes_blobs_and_refs_returns_counts(self):
        store, files, refs, bucket, _stream = _make_store()

        async def _afind(_query, projection=None):
            for doc in [{"_id": "a"}, {"_id": "b"}]:
                yield doc

        files.find = MagicMock(return_value=_afind(None))

        counts = await store.delete_by_channel(CHANNEL)
        assert counts == {"blobs_deleted": 2, "refs_deleted": 3}
        assert bucket.delete.await_count == 2
        refs.delete_many.assert_awaited_once_with({"channel_id": CHANNEL})

    async def test_never_raises_on_failure(self):
        store, files, refs, bucket, _stream = _make_store()
        files.find = MagicMock(side_effect=RuntimeError("scan boom"))
        refs.delete_many = AsyncMock(side_effect=RuntimeError("ref boom"))
        # Must swallow both failures and return partial counts.
        counts = await store.delete_by_channel(CHANNEL)
        assert counts == {"blobs_deleted": 0, "refs_deleted": 0}


# ---------------------------------------------------------------------------
# Live mongo:7 integration — gated on a pingable mongod
# ---------------------------------------------------------------------------


# NOTE: this gate runs at COLLECTION time (inside the skipif expression), i.e.
# before any test fixture — including conftest's ``_auth_bypass`` env setup —
# has run. It must therefore never call ``get_settings()``: doing so would
# prime the lru_cached Settings singleton from the raw environment and break
# every later test that relies on ``BEEVER_API_KEYS=test-key`` (401s). Read
# the URI straight from the environment instead (CI sets MONGODB_URI; local
# dev falls back to the docker-compose default).
_MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/beever_atlas")


def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient

        client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=1000)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


_skip_without_mongo = pytest.mark.skipif(
    not _mongo_available(),
    reason="no mongod answered MONGODB_URI (CI runs mongo:7)",
)


@_skip_without_mongo
class TestMediaBlobStoreIntegration:
    """End-to-end round-trips against a real GridFS bucket.

    Uses an isolated test database so it never touches ``beever_atlas`` and
    drops itself on teardown, mirroring the conftest chat-history test-DB
    convention.
    """

    @pytest.fixture
    async def store(self):
        from motor.motor_asyncio import AsyncIOMotorClient

        db_name = f"beever_atlas_test_media_{uuid.uuid4().hex[:8]}"
        client = AsyncIOMotorClient(_MONGO_URI)
        db = client[db_name]
        blob_store = MediaBlobStore(MagicMock(db=db))
        await blob_store.startup()
        try:
            yield blob_store
        finally:
            await client.drop_database(db_name)
            client.close()

    async def test_save_then_open_by_url_roundtrip(self, store):
        sha = await store.save_blob(
            content=SAMPLE,
            mime_type=MIME,
            filename=FILENAME,
            channel_id=CHANNEL,
            source_id=SOURCE,
            message_id=MESSAGE,
            platform_url=DISCORD_URL,
        )
        assert sha == SAMPLE_SHA

        # Re-signed URL resolves to the same stored bytes (host+path identity).
        # open_by_url now returns a backend-neutral (BlobRead, ref) pair.
        result = await store.open_by_url(DISCORD_URL_RESIGNED)
        assert result is not None
        read, ref = result
        data = b""
        async for chunk in read.iterator:
            data += chunk
        assert data == SAMPLE
        assert ref["sha256"] == SAMPLE_SHA
        assert ref["channel_id"] == CHANNEL

    async def test_dedup_single_blob_per_sha_channel(self, store):
        for url in (DISCORD_URL, DISCORD_URL_RESIGNED):
            await store.save_blob(
                content=SAMPLE,
                mime_type=MIME,
                filename=FILENAME,
                channel_id=CHANNEL,
                source_id=SOURCE,
                message_id=MESSAGE,
                platform_url=url,
            )
        stats = await store.stats()
        assert stats["total_blobs"] == 1  # deduped on (sha256, channel_id)
        assert stats["total_bytes"] == len(SAMPLE)

    async def test_has_ref_and_purge(self, store):
        await store.save_blob(
            content=SAMPLE,
            mime_type=MIME,
            filename=FILENAME,
            channel_id=CHANNEL,
            source_id=SOURCE,
            message_id=MESSAGE,
            platform_url=DISCORD_URL,
        )
        assert await store.has_ref(DISCORD_URL, CHANNEL) is True

        counts = await store.delete_by_channel(CHANNEL)
        assert counts["blobs_deleted"] == 1
        assert counts["refs_deleted"] == 1
        assert await store.has_ref(DISCORD_URL, CHANNEL) is False
        assert await store.open_by_url(DISCORD_URL) is None
