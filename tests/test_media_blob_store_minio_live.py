"""Live end-to-end test: MediaBlobStore over real Mongo (refs) + real MinIO (bytes).

This proves the backend split at the seam that matters: with
``CHANNEL_MEDIA_BACKEND=minio``, the ``channel_media_refs`` document still lands
in Mongo (the refs / url_key / Telegram layer is backend-agnostic) while the raw
bytes go to MinIO under the ``channels/{channel_id}/{sha256}`` key, and a
re-signed URL still resolves through the Mongo ref to the MinIO object.

Gating: the skipif runs at COLLECTION time — before any fixture, including
conftest's ``_auth_bypass``. It MUST NOT call ``get_settings()`` (that would
prime the lru_cached Settings from the raw env and 401 every later auth test).
Instead it reads ``CHANNEL_MEDIA_MINIO_ENDPOINT``/``MINIO_ENDPOINT`` and
``MONGODB_URI`` straight from ``os.environ`` and probes both with short
timeouts, skipping cleanly when either is unreachable. To run locally::

    docker compose --profile minio up -d minio minio-init mongodb
    CHANNEL_MEDIA_MINIO_ENDPOINT=http://localhost:9000 \\
    CHANNEL_MEDIA_MINIO_ACCESS_KEY=minioadmin \\
    CHANNEL_MEDIA_MINIO_SECRET_KEY=<secret> \\
    MONGODB_URI=mongodb://localhost:27017/beever_atlas \\
    pytest tests/test_media_blob_store_minio_live.py

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import hashlib
import os
import socket
import uuid
from urllib.parse import urlparse

import pytest

from beever_atlas.stores.blob_backend import blob_prefix
from beever_atlas.stores.media_blob_store import MediaBlobStore
from beever_atlas.stores.minio_backend import MinioBackend


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the Settings cache around every test in this module (see siblings)."""
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Collection-time gating (NO get_settings() — read os.environ directly)
# ---------------------------------------------------------------------------

_MINIO_ENDPOINT = os.environ.get("CHANNEL_MEDIA_MINIO_ENDPOINT") or os.environ.get("MINIO_ENDPOINT")
_MINIO_ACCESS_KEY = os.environ.get("CHANNEL_MEDIA_MINIO_ACCESS_KEY", "minioadmin")
_MINIO_SECRET_KEY = os.environ.get("CHANNEL_MEDIA_MINIO_SECRET_KEY", "")
_MONGO_URI = os.environ.get("MONGODB_URI", "mongodb://localhost:27017/beever_atlas")


def _tcp_reachable(host: str | None, port: int | None) -> bool:
    if not host or not port:
        return False
    try:
        with socket.create_connection((host, port), timeout=1.0):
            return True
    except OSError:
        return False


def _minio_available() -> bool:
    if not _MINIO_ENDPOINT:
        return False
    parsed = urlparse(_MINIO_ENDPOINT)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return _tcp_reachable(parsed.hostname, port)


def _mongo_available() -> bool:
    try:
        from pymongo import MongoClient

        client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=1000)
        client.admin.command("ping")
        client.close()
        return True
    except Exception:
        return False


_skip_without_minio = pytest.mark.skipif(
    not (_minio_available() and _mongo_available()),
    reason="needs a reachable CHANNEL_MEDIA_MINIO_ENDPOINT + MONGODB_URI",
)


# ---------------------------------------------------------------------------
# Shared sample data (mirrors tests/test_media_blob_store.py)
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
DISCORD_URL_KEY = "cdn.discordapp.com/attachments/1/2/shot.png"

# Per-platform (url, resigned, expected url_key) shapes for the round-trip
# matrix: a stored blob keyed on host+path survives token/signature rotation.
SLACK_URL = "https://files.slack.com/files-pri/T1-F1/shot.png?t=AAA"
SLACK_URL_RESIGNED = "https://files.slack.com/files-pri/T1-F1/shot.png?t=BBB"
SLACK_URL_KEY = "files.slack.com/files-pri/T1-F1/shot.png"

TEAMS_URL = "https://graph.microsoft.com/v1.0/drives/D1/items/I1/content?tempauth=AAA"
TEAMS_URL_RESIGNED = "https://graph.microsoft.com/v1.0/drives/D1/items/I1/content?tempauth=ZZZ"
TEAMS_URL_KEY = "graph.microsoft.com/v1.0/drives/D1/items/I1/content"

# Mattermost is extensionless and carries no rotating query — the identity IS
# host+path, so the "re-signed" URL is the same URL.
MM_URL = "https://team.example.com/api/v4/files/fileid123"
MM_URL_KEY = "team.example.com/api/v4/files/fileid123"


@_skip_without_minio
class TestMediaBlobStoreMinioLive:
    """End-to-end round-trips: refs in Mongo, bytes in MinIO."""

    @pytest.fixture
    async def store(self):
        from unittest.mock import MagicMock

        from motor.motor_asyncio import AsyncIOMotorClient

        db_name = f"beever_atlas_test_media_minio_{uuid.uuid4().hex[:8]}"
        bucket = f"atlas-media-test-{uuid.uuid4().hex[:8]}"
        client = AsyncIOMotorClient(_MONGO_URI)
        db = client[db_name]

        backend = MinioBackend(
            endpoint_url=_MINIO_ENDPOINT,
            access_key=_MINIO_ACCESS_KEY,
            secret_key=_MINIO_SECRET_KEY,
            bucket=bucket,
            secure=urlparse(_MINIO_ENDPOINT or "").scheme == "https",
            region="us-east-1",
        )
        blob_store = MediaBlobStore(MagicMock(db=db), backend=backend)
        await blob_store.startup()
        try:
            yield blob_store
        finally:
            # Purge the test bucket's objects (best-effort) then drop the DB.
            try:
                await backend.delete_prefix("channels/")
            except Exception:
                pass
            await blob_store.aclose()
            await client.drop_database(db_name)
            client.close()

    async def _save(self, store, *, url: str = DISCORD_URL) -> str:
        return await store.save_blob(
            content=SAMPLE,
            mime_type=MIME,
            filename=FILENAME,
            channel_id=CHANNEL,
            source_id=SOURCE,
            message_id=MESSAGE,
            platform_url=url,
        )

    async def test_save_then_open_by_url_roundtrip(self, store):
        sha = await self._save(store)
        assert sha == SAMPLE_SHA

        # The ref MUST be in Mongo even though the bytes went to MinIO.
        ref_doc = await store._db["channel_media_refs"].find_one(
            {"url_key": DISCORD_URL_KEY, "channel_id": CHANNEL}
        )
        assert ref_doc is not None
        assert ref_doc["sha256"] == SAMPLE_SHA
        assert ref_doc["channel_id"] == CHANNEL

        # A re-signed URL resolves through the same Mongo ref to the MinIO object.
        result = await store.open_by_url(DISCORD_URL_RESIGNED)
        assert result is not None
        read, ref = result
        data = b""
        async for chunk in read.iterator:
            data += chunk
        assert data == SAMPLE
        assert ref["sha256"] == SAMPLE_SHA

    @pytest.mark.parametrize(
        ("url", "resigned", "expected_key"),
        [
            (SLACK_URL, SLACK_URL_RESIGNED, SLACK_URL_KEY),
            (DISCORD_URL, DISCORD_URL_RESIGNED, DISCORD_URL_KEY),
            (TEAMS_URL, TEAMS_URL_RESIGNED, TEAMS_URL_KEY),
            (MM_URL, MM_URL, MM_URL_KEY),  # extensionless, same-url round-trip
        ],
        ids=["slack", "discord", "teams", "mattermost"],
    )
    async def test_save_then_open_by_resigned_url_per_platform(
        self, store, url, resigned, expected_key
    ):
        """Every platform's real URL shape: save → open_by_url(re-signed) →
        same bytes, and the ref keys on the stable host+path identity."""
        sha = await self._save(store, url=url)
        assert sha == SAMPLE_SHA

        result = await store.open_by_url(resigned)
        assert result is not None
        read, ref = result
        data = b""
        async for chunk in read.iterator:
            data += chunk
        assert data == SAMPLE
        assert ref["sha256"] == SAMPLE_SHA
        assert ref["url_key"] == expected_key

    async def test_dedup_single_object_per_sha_channel(self, store):
        for url in (DISCORD_URL, DISCORD_URL_RESIGNED):
            await self._save(store, url=url)
        stats = await store.stats()
        assert stats["total_blobs"] == 1  # exists()-probe skipped the 2nd put
        assert stats["total_bytes"] == len(SAMPLE)

    async def test_stats_and_delete_by_channel_removes_objects(self, store):
        await self._save(store)
        stats = await store.stats()
        assert stats["total_blobs"] >= 1
        assert stats["total_bytes"] >= len(SAMPLE)

        counts = await store.delete_by_channel(CHANNEL)
        assert counts == {"blobs_deleted": 1, "refs_deleted": 1}

        assert await store.has_ref(DISCORD_URL, CHANNEL) is False
        assert await store.open_by_url(DISCORD_URL) is None
        # The MinIO objects under the channel prefix are gone (not just the ref).
        remaining, _bytes = await store._backend.stats()
        assert remaining == 0
        gone = await store._backend.delete_prefix(blob_prefix(CHANNEL))
        assert gone == 0
