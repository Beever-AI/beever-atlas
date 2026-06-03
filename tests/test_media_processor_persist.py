"""Unit tests for the durable-media persist hook in ``MediaProcessor``.

Covers the best-effort contract: bytes are saved to the blob store right
after download and before extraction, but a storage failure (raise, missing
store, disabled flag, no bytes) must NEVER break extraction — the description
is still returned.

Each download site is exercised:
  * ``_process_attachment`` (the live ingestion path)
  * ``_handle_pdf`` / ``_handle_image`` (independent download helpers)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.services.media_extractors import MediaContent
from beever_atlas.services.media_processor import MediaContext, MediaProcessor


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the Settings cache around every test in this module.

    ``get_settings()`` is an ``lru_cache`` singleton. These tests instantiate
    ``MediaProcessor()`` (which caches ``get_settings()`` as ``_settings``)
    and toggle ``channel_media_persist`` on that instance, so the cache must
    be (a) built AFTER conftest's ``_auth_bypass`` sets the test env vars and
    (b) discarded afterwards so the flag mutation never leaks into other test
    modules. Module-level autouse fixtures run after conftest autouse
    fixtures, which guarantees (a) regardless of conftest-internal ordering.
    """
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_BYTES = b"fake-file-bytes"
SAMPLE_DESCRIPTION = "Extracted description of the attachment."
PLATFORM_URL = "https://cdn.discordapp.com/attachments/1/2/chart.png?ex=abc&is=def"
CHANNEL_ID = "C123"
MESSAGE_ID = "1700000000.000100"
SOURCE_ID = "discord"


def _make_blob_store(save_raises: bool = False) -> MagicMock:
    """Return a mock ``MediaBlobStore`` with an awaitable ``save_blob``."""
    store = MagicMock()
    if save_raises:
        store.save_blob = AsyncMock(side_effect=Exception("mongo down"))
    else:
        store.save_blob = AsyncMock(return_value="deadbeef")
    return store


def _make_stores(blob_store: MagicMock | None) -> MagicMock:
    """Return a mock ``StoreClients`` exposing ``media_blob_store``."""
    stores = MagicMock()
    # MagicMock auto-creates attributes; set explicitly so ``getattr`` returns
    # exactly what the test intends (including ``None``).
    stores.media_blob_store = blob_store
    return stores


def _make_extractor() -> MagicMock:
    """Registry extractor whose ``extract`` returns a fixed description."""
    extractor = MagicMock()
    extractor.extract = AsyncMock(
        return_value=MediaContent(text=SAMPLE_DESCRIPTION, media_type="image")
    )
    return extractor


def _patch_processor(
    proc: MediaProcessor,
    *,
    stores: MagicMock,
    persist_enabled: bool = True,
    download_bytes: bytes | None = SAMPLE_BYTES,
):
    """Build the patch context: stores, settings flag, download, registry."""
    # Toggle the persist flag on the processor's cached settings.
    proc._settings.channel_media_persist = persist_enabled

    registry = MagicMock()
    registry.get_extractor = MagicMock(return_value=_make_extractor())

    return (
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch.object(proc, "get_registry", return_value=registry),
        patch.object(proc, "_download_file", new=AsyncMock(return_value=download_bytes)),
    )


# ---------------------------------------------------------------------------
# _process_attachment — the live path
# ---------------------------------------------------------------------------


class TestProcessAttachmentPersist:
    @pytest.mark.asyncio
    async def test_persist_called_with_correct_args(self):
        """On a successful download, save_blob is called with the full ctx."""
        proc = MediaProcessor()
        blob_store = _make_blob_store()
        stores = _make_stores(blob_store)
        p_stores, p_registry, p_download = _patch_processor(proc, stores=stores)

        ctx = MediaContext(channel_id=CHANNEL_ID, message_id=MESSAGE_ID, source_id=SOURCE_ID)
        with p_stores, p_registry, p_download:
            result = await proc._process_attachment(
                {"url": PLATFORM_URL, "name": "chart.png", "type": "image"},
                "see attached",
                ctx=ctx,
            )

        assert SAMPLE_DESCRIPTION in result["description"]
        blob_store.save_blob.assert_awaited_once()
        kwargs = blob_store.save_blob.call_args.kwargs
        assert kwargs["content"] == SAMPLE_BYTES
        assert kwargs["mime_type"] == "image/png"
        assert kwargs["filename"] == "chart.png"
        assert kwargs["channel_id"] == CHANNEL_ID
        assert kwargs["message_id"] == MESSAGE_ID
        assert kwargs["source_id"] == SOURCE_ID
        assert kwargs["platform_url"] == PLATFORM_URL

    @pytest.mark.asyncio
    async def test_persist_skipped_when_flag_disabled(self):
        """channel_media_persist=False short-circuits before touching stores."""
        proc = MediaProcessor()
        blob_store = _make_blob_store()
        stores = _make_stores(blob_store)
        p_stores, p_registry, p_download = _patch_processor(
            proc, stores=stores, persist_enabled=False
        )

        with p_stores, p_registry, p_download:
            result = await proc._process_attachment(
                {"url": PLATFORM_URL, "name": "chart.png", "type": "image"},
                "see attached",
                ctx=MediaContext(channel_id=CHANNEL_ID),
            )

        assert SAMPLE_DESCRIPTION in result["description"]
        blob_store.save_blob.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_persist_skipped_when_download_returns_none(self):
        """No bytes → no extraction and no persist."""
        proc = MediaProcessor()
        blob_store = _make_blob_store()
        stores = _make_stores(blob_store)
        p_stores, p_registry, p_download = _patch_processor(
            proc, stores=stores, download_bytes=None
        )

        with p_stores, p_registry, p_download:
            result = await proc._process_attachment(
                {"url": PLATFORM_URL, "name": "chart.png", "type": "image"},
                "see attached",
                ctx=MediaContext(channel_id=CHANNEL_ID),
            )

        # Download miss returns the metadata-only fallback, never persists.
        blob_store.save_blob.assert_not_awaited()
        assert result["media_url"] == PLATFORM_URL

    @pytest.mark.asyncio
    async def test_save_blob_exception_does_not_break_extraction(self):
        """A storage failure is swallowed; the description still returns."""
        proc = MediaProcessor()
        blob_store = _make_blob_store(save_raises=True)
        stores = _make_stores(blob_store)
        p_stores, p_registry, p_download = _patch_processor(proc, stores=stores)

        with p_stores, p_registry, p_download:
            result = await proc._process_attachment(
                {"url": PLATFORM_URL, "name": "chart.png", "type": "image"},
                "see attached",
                ctx=MediaContext(channel_id=CHANNEL_ID),
            )

        blob_store.save_blob.assert_awaited_once()
        assert SAMPLE_DESCRIPTION in result["description"]

    @pytest.mark.asyncio
    async def test_missing_blob_store_does_not_break_extraction(self):
        """media_blob_store=None (partial init) is tolerated."""
        proc = MediaProcessor()
        stores = _make_stores(None)
        p_stores, p_registry, p_download = _patch_processor(proc, stores=stores)

        with p_stores, p_registry, p_download:
            result = await proc._process_attachment(
                {"url": PLATFORM_URL, "name": "chart.png", "type": "image"},
                "see attached",
                ctx=MediaContext(channel_id=CHANNEL_ID),
            )

        assert SAMPLE_DESCRIPTION in result["description"]

    @pytest.mark.asyncio
    async def test_get_stores_raising_does_not_break_extraction(self):
        """Stores not initialized (RuntimeError) is tolerated."""
        proc = MediaProcessor()
        proc._settings.channel_media_persist = True

        registry = MagicMock()
        registry.get_extractor = MagicMock(return_value=_make_extractor())

        with (
            patch(
                "beever_atlas.stores.get_stores",
                side_effect=RuntimeError("Stores not initialized"),
            ),
            patch.object(proc, "get_registry", return_value=registry),
            patch.object(proc, "_download_file", new=AsyncMock(return_value=SAMPLE_BYTES)),
        ):
            result = await proc._process_attachment(
                {"url": PLATFORM_URL, "name": "chart.png", "type": "image"},
                "see attached",
                ctx=MediaContext(channel_id=CHANNEL_ID),
            )

        assert SAMPLE_DESCRIPTION in result["description"]


# ---------------------------------------------------------------------------
# process_message_media — context derivation end-to-end
# ---------------------------------------------------------------------------


class TestProcessMessageMediaContext:
    @pytest.mark.asyncio
    async def test_message_context_threaded_into_save_blob(self):
        """The msg dict's channel/message/platform fields reach save_blob."""
        proc = MediaProcessor()
        blob_store = _make_blob_store()
        stores = _make_stores(blob_store)
        p_stores, p_registry, p_download = _patch_processor(proc, stores=stores)

        msg = {
            "channel_id": CHANNEL_ID,
            "ts": MESSAGE_ID,
            "platform": SOURCE_ID,
            "text": "see attached",
            "modality": "mixed",
            "attachments": [{"url": PLATFORM_URL, "name": "chart.png", "type": "image"}],
        }

        with p_stores, p_registry, p_download:
            await proc.process_message_media(msg)

        blob_store.save_blob.assert_awaited_once()
        kwargs = blob_store.save_blob.call_args.kwargs
        assert kwargs["channel_id"] == CHANNEL_ID
        # message_id falls back to ``ts`` when no explicit message_id is set.
        assert kwargs["message_id"] == MESSAGE_ID
        assert kwargs["source_id"] == SOURCE_ID
        assert kwargs["platform_url"] == PLATFORM_URL


# ---------------------------------------------------------------------------
# _handle_pdf / _handle_image — independent download sites
# ---------------------------------------------------------------------------


class TestIndependentDownloadSitesPersist:
    @pytest.mark.asyncio
    async def test_handle_pdf_persists_after_download(self):
        proc = MediaProcessor()
        proc._settings.channel_media_persist = True
        blob_store = _make_blob_store()
        stores = _make_stores(blob_store)

        pdf_extractor = MagicMock()
        pdf_extractor.extract = AsyncMock(
            return_value=MediaContent(text="pdf text", media_type="pdf")
        )

        with (
            patch("beever_atlas.stores.get_stores", return_value=stores),
            patch.object(proc, "_download_file", new=AsyncMock(return_value=SAMPLE_BYTES)),
            patch(
                "beever_atlas.services.media_extractors.PdfExtractor",
                return_value=pdf_extractor,
            ),
        ):
            result = await proc._handle_pdf(
                PLATFORM_URL,
                "doc.pdf",
                ctx=MediaContext(channel_id=CHANNEL_ID, source_id=SOURCE_ID),
            )

        blob_store.save_blob.assert_awaited_once()
        kwargs = blob_store.save_blob.call_args.kwargs
        assert kwargs["mime_type"] == "application/pdf"
        assert kwargs["channel_id"] == CHANNEL_ID
        assert result["description"] == "pdf text"

    @pytest.mark.asyncio
    async def test_handle_pdf_persist_failure_does_not_break(self):
        proc = MediaProcessor()
        proc._settings.channel_media_persist = True
        blob_store = _make_blob_store(save_raises=True)
        stores = _make_stores(blob_store)

        pdf_extractor = MagicMock()
        pdf_extractor.extract = AsyncMock(
            return_value=MediaContent(text="pdf text", media_type="pdf")
        )

        with (
            patch("beever_atlas.stores.get_stores", return_value=stores),
            patch.object(proc, "_download_file", new=AsyncMock(return_value=SAMPLE_BYTES)),
            patch(
                "beever_atlas.services.media_extractors.PdfExtractor",
                return_value=pdf_extractor,
            ),
        ):
            result = await proc._handle_pdf(PLATFORM_URL, "doc.pdf")

        assert result["description"] == "pdf text"

    @pytest.mark.asyncio
    async def test_handle_image_persists_after_download(self):
        proc = MediaProcessor()
        proc._settings.channel_media_persist = True
        blob_store = _make_blob_store()
        stores = _make_stores(blob_store)

        with (
            patch("beever_atlas.stores.get_stores", return_value=stores),
            patch.object(proc, "_download_file", new=AsyncMock(return_value=SAMPLE_BYTES)),
            patch.object(proc, "_describe_image", new=AsyncMock(return_value="a chart")),
        ):
            # Short text → should_use_vision True → image is downloaded.
            result = await proc._handle_image(
                PLATFORM_URL,
                "chart.png",
                "hi",
                ctx=MediaContext(channel_id=CHANNEL_ID),
            )

        blob_store.save_blob.assert_awaited_once()
        kwargs = blob_store.save_blob.call_args.kwargs
        assert kwargs["mime_type"] == "image/png"
        assert kwargs["channel_id"] == CHANNEL_ID
        assert "a chart" in result["description"]

    @pytest.mark.asyncio
    async def test_handle_image_no_download_no_persist_when_vision_skipped(self):
        """Vision-skip path never downloads, so it never persists."""
        proc = MediaProcessor()
        proc._settings.channel_media_persist = True
        blob_store = _make_blob_store()
        stores = _make_stores(blob_store)

        download = AsyncMock(return_value=SAMPLE_BYTES)
        with (
            patch("beever_atlas.stores.get_stores", return_value=stores),
            patch.object(proc, "_download_file", new=download),
        ):
            # Long substantive text with no attachment cue → vision skipped.
            await proc._handle_image(PLATFORM_URL, "photo.png", "x" * 200)

        download.assert_not_awaited()
        blob_store.save_blob.assert_not_awaited()
