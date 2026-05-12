"""Unit tests for P0-2 media extractor content-hash cache.

Covers:
  (a) cache miss  → Gemini call fires + result written to cache
  (b) cache hit   → no Gemini call, cached description returned
  (c) cache write failure does NOT block extraction return
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.services.media_extractors import (
    ImageExtractor,
    _compute_media_hash,
)
from beever_atlas.stores.media_cache_store import CachedMedia, MediaCacheStore


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_DATA = b"fake-image-bytes"
SAMPLE_FILENAME = "screenshot.png"
SAMPLE_MIME = "image/png"
SAMPLE_DESCRIPTION = "A screenshot showing a dashboard."


def _make_mock_collection(find_one_result=None):
    """Return a mock AsyncIOMotorCollection."""
    col = MagicMock()
    col.find_one = AsyncMock(return_value=find_one_result)
    col.update_one = AsyncMock(return_value=None)
    col.create_index = AsyncMock(return_value="media_cache_hash_mime_unique")
    return col


# ---------------------------------------------------------------------------
# MediaCacheStore unit tests
# ---------------------------------------------------------------------------


class TestMediaCacheStore:
    @pytest.mark.asyncio
    async def test_get_cached_miss_returns_none(self):
        store = MediaCacheStore(_make_mock_collection(find_one_result=None))
        result = await store.get_cached("deadbeef", "image/png")
        assert result is None

    @pytest.mark.asyncio
    async def test_get_cached_hit_returns_cached_media(self):
        doc = {"description": SAMPLE_DESCRIPTION, "model_version": "gemini-2.5-flash"}
        store = MediaCacheStore(_make_mock_collection(find_one_result=doc))
        result = await store.get_cached("deadbeef", "image/png")
        assert result is not None
        assert result.description == SAMPLE_DESCRIPTION
        assert result.model_version == "gemini-2.5-flash"

    @pytest.mark.asyncio
    async def test_set_cached_calls_update_one(self):
        col = _make_mock_collection()
        store = MediaCacheStore(col)
        await store.set_cached("deadbeef", "image/png", SAMPLE_DESCRIPTION, "gemini-2.5-flash")
        col.update_one.assert_awaited_once()
        call_args = col.update_one.call_args
        # First positional arg is the filter
        assert call_args[0][0] == {"hash": "deadbeef", "mime_type": "image/png"}


# ---------------------------------------------------------------------------
# _compute_media_hash
# ---------------------------------------------------------------------------


class TestComputeMediaHash:
    def test_hash_is_64_hex_chars(self):
        import hashlib

        from beever_atlas.infra.config import get_settings

        settings = get_settings()
        version_bytes = str(settings.media_cache_version).encode()
        expected = hashlib.sha256(SAMPLE_DATA + version_bytes).hexdigest()
        assert _compute_media_hash(SAMPLE_DATA) == expected

    def test_different_data_different_hash(self):
        h1 = _compute_media_hash(b"aaa")
        h2 = _compute_media_hash(b"bbb")
        assert h1 != h2


# ---------------------------------------------------------------------------
# ImageExtractor cache integration
# ---------------------------------------------------------------------------


class TestImageExtractorCache:
    """Integration tests for cache miss / hit / write-failure paths."""

    def _make_stores_with_cache(self, cached_media=None, write_raises=False):
        """Return a mock StoreClients whose mongodb.media_cache behaves as spec'd."""
        cache_store = MagicMock()
        cache_store.get_cached = AsyncMock(return_value=cached_media)
        if write_raises:
            cache_store.set_cached = AsyncMock(side_effect=Exception("mongo down"))
        else:
            cache_store.set_cached = AsyncMock(return_value=None)

        mongodb = MagicMock()
        mongodb.media_cache = cache_store

        stores = MagicMock()
        stores.mongodb = mongodb
        return stores

    @pytest.mark.asyncio
    async def test_cache_miss_calls_gemini_and_writes_cache(self):
        """On miss: Gemini is called, result is written to cache."""
        extractor = ImageExtractor()
        stores = self._make_stores_with_cache(cached_media=None)

        with (
            patch(
                "beever_atlas.stores.get_stores",
                return_value=stores,
            ),
            patch.object(
                extractor,
                "_describe_image",
                new=AsyncMock(return_value=SAMPLE_DESCRIPTION),
            ),
        ):
            result = await extractor.extract(
                SAMPLE_DATA,
                SAMPLE_FILENAME,
                metadata={"message_text": "see attached"},
            )

        assert SAMPLE_DESCRIPTION in result.text
        stores.mongodb.media_cache.set_cached.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cache_hit_skips_gemini(self):
        """On hit: Gemini is NOT called, cached description is returned."""
        extractor = ImageExtractor()
        stores = self._make_stores_with_cache(
            cached_media=CachedMedia(
                description=SAMPLE_DESCRIPTION, model_version="gemini-2.5-flash"
            )
        )

        with (
            patch(
                "beever_atlas.stores.get_stores",
                return_value=stores,
            ),
            patch.object(
                extractor,
                "_describe_image",
                new=AsyncMock(return_value="should not be called"),
            ) as mock_describe,
        ):
            result = await extractor.extract(
                SAMPLE_DATA,
                SAMPLE_FILENAME,
                metadata={"message_text": "see attached"},
            )

        mock_describe.assert_not_awaited()
        assert result.text == SAMPLE_DESCRIPTION

    @pytest.mark.asyncio
    async def test_cache_write_failure_does_not_block_extraction(self):
        """Cache write failure must not propagate — extraction result is still returned."""
        extractor = ImageExtractor()
        stores = self._make_stores_with_cache(cached_media=None, write_raises=True)

        with (
            patch(
                "beever_atlas.stores.get_stores",
                return_value=stores,
            ),
            patch.object(
                extractor,
                "_describe_image",
                new=AsyncMock(return_value=SAMPLE_DESCRIPTION),
            ),
        ):
            result = await extractor.extract(
                SAMPLE_DATA,
                SAMPLE_FILENAME,
                metadata={"message_text": "see attached"},
            )

        # Result is still returned despite write failure
        assert SAMPLE_DESCRIPTION in result.text

    @pytest.mark.asyncio
    async def test_vision_gate_skip_never_computes_hash(self):
        """Images failing _should_use_vision must not compute SHA-256 (architect AC)."""
        extractor = ImageExtractor()

        with patch("beever_atlas.services.media_extractors._compute_media_hash") as mock_hash:
            # Long message text with no attachment cues → _should_use_vision returns False
            long_text = "x" * 200
            result = await extractor.extract(
                SAMPLE_DATA,
                "photo.png",
                metadata={"message_text": long_text},
            )

        mock_hash.assert_not_called()
        assert result.text == "[Attachment: photo.png (image)]"
