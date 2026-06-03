"""Unit tests for the durable channel-media backfill service.

Mocks the Motor surface and the store singleton (``MagicMock`` collections +
``AsyncMock`` methods, no real Mongo) in the style of ``tests/test_media_cache.py``.
The ``get_stores`` singleton used inside
``beever_atlas.services.media_backfill`` is monkeypatched to a fake whose
``mongodb._channel_messages`` returns a scripted cursor, whose
``media_blob_store`` records ``has_ref`` / ``save_blob`` calls, and whose
``platform.list_connections`` drives the connection_id resolution.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import beever_atlas.services.media_backfill as mb
from beever_atlas.services.media_backfill import BackfillReport, backfill_channel_media


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the Settings cache around every test in this module.

    Keeps this module from depending on (or corrupting) the process-wide
    ``get_settings()`` lru_cache — see test_media_blob_store.py for the full
    rationale on the conftest ``_auth_bypass`` / cache-priming interaction.
    """
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


CHANNEL = "C123"
SOURCE = "slack:T1"
CONN_ID = "conn-1"
DISCORD_URL = "https://cdn.discordapp.com/attachments/1/2/shot.png?ex=abc&is=def"
TELEGRAM_URL = "https://api.telegram.org/file/bot12345:TOKEN/photos/a.jpg"
SAMPLE = b"fake-image-bytes"


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeCursor:
    """Async-iterable cursor honouring ``.sort().limit()`` chaining.

    Each ``find(query)`` returns a fresh cursor over the docs whose ``_id`` is
    greater than the query's ``$gt`` bound (mirrors the real keyset scan) so
    the resume cursor advances and the loop terminates.
    """

    def __init__(self, docs: list[dict[str, Any]], query: dict[str, Any]) -> None:
        gt = (query.get("_id") or {}).get("$gt")
        self._docs = [d for d in docs if gt is None or d["_id"] > gt]
        self._limit = len(self._docs)

    def sort(self, *_args: Any, **_kwargs: Any) -> _FakeCursor:
        return self

    def limit(self, n: int) -> _FakeCursor:
        self._limit = n
        return self

    def __aiter__(self):
        async def _gen():
            for doc in self._docs[: self._limit]:
                yield doc

        return _gen()


class _FakeMessages:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs
        self.find_queries: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        self.find_queries.append(query)
        return _FakeCursor(self._docs, query)


def _make_doc(_id: int, attachments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "_id": _id,
        "channel_id": CHANNEL,
        "source_id": SOURCE,
        "message_id": f"M{_id}",
        "attachments": attachments,
    }


def _install_stores(
    monkeypatch: pytest.MonkeyPatch,
    *,
    docs: list[dict[str, Any]],
    has_ref: bool = False,
    connections: list[Any] | None = None,
) -> tuple[MagicMock, _FakeMessages, dict[str, MagicMock]]:
    """Wire a fake store singleton + a fake MediaProcessor download path.

    Returns (stores, messages, state_cols) so assertions can inspect calls and
    the persisted resume state.
    """
    messages = _FakeMessages(docs)

    # State + resume collection — a single shared MagicMock per ``db[name]``.
    state_col = MagicMock(name="media_backfill_state")
    state_col.find_one = AsyncMock(return_value=None)
    state_col.update_one = AsyncMock(return_value=None)
    state_col.delete_one = AsyncMock(return_value=None)

    db: dict[str, Any] = {"media_backfill_state": state_col}

    mongodb = MagicMock()
    mongodb._channel_messages = messages
    mongodb.db = db

    blob_store = MagicMock(name="media_blob_store")
    blob_store.has_ref = AsyncMock(return_value=has_ref)
    blob_store.save_blob = AsyncMock(return_value="sha")

    platform = MagicMock()
    platform.list_connections = AsyncMock(return_value=connections or [])

    stores = MagicMock()
    stores.mongodb = mongodb
    stores.media_blob_store = blob_store
    stores.platform = platform

    monkeypatch.setattr(mb, "get_stores", lambda: stores)
    return stores, messages, {"state": state_col}


def _install_download(monkeypatch: pytest.MonkeyPatch, *, returns: bytes | None) -> MagicMock:
    """Patch MediaProcessor so no real httpx/bridge call happens.

    Returns the download mock so tests can assert call count + connection_id.
    """
    download = AsyncMock(return_value=returns)
    monkeypatch.setattr(mb.MediaProcessor, "_download_file", download)
    monkeypatch.setattr(mb.MediaProcessor, "close", AsyncMock(return_value=None))
    # Pass ``returns=mb.OVERSIZE`` to simulate a size-cap rejection; the
    # downloader is what classifies oversize (via the sentinel), so the backfill
    # routes it to ``too_large`` without any size check of its own.
    return download


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------


class TestBackfillReport:
    def test_to_dict_shape(self):
        report = BackfillReport(channel_id=CHANNEL, dry_run=True)
        report.record(SOURCE, "stored")
        report.record(SOURCE, "no_url")
        d = report.to_dict()
        assert d["channel_id"] == CHANNEL
        assert d["dry_run"] is True
        assert d["stored"] == 1
        assert d["no_url"] == 1
        assert set(d) >= {
            "messages_scanned",
            "stored",
            "already_stored",
            "no_url",
            "download_failed",
            "too_large",
            "skipped_telegram",
            "errors",
            "by_platform",
        }
        # Per-platform mirror.
        assert d["by_platform"][SOURCE]["stored"] == 1
        assert d["by_platform"][SOURCE]["no_url"] == 1


# ---------------------------------------------------------------------------
# backfill_channel_media
# ---------------------------------------------------------------------------


class TestBackfill:
    async def test_dry_run_counts_without_downloading(self, monkeypatch):
        docs = [_make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}])]
        _install_stores(monkeypatch, docs=docs)
        download = _install_download(monkeypatch, returns=SAMPLE)

        report = await backfill_channel_media(channel_id=CHANNEL, dry_run=True)

        assert report.stored == 1  # counted as "would store"
        download.assert_not_called()  # no download in dry_run

    async def test_already_stored_refs_skipped(self, monkeypatch):
        docs = [_make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}])]
        _install_stores(monkeypatch, docs=docs, has_ref=True)
        download = _install_download(monkeypatch, returns=SAMPLE)

        report = await backfill_channel_media(channel_id=CHANNEL)

        assert report.already_stored == 1
        assert report.stored == 0
        download.assert_not_called()

    async def test_no_url_counted(self, monkeypatch):
        docs = [_make_doc(1, [{"name": "a.png"}])]  # no url/url_private
        _install_stores(monkeypatch, docs=docs)
        download = _install_download(monkeypatch, returns=SAMPLE)

        report = await backfill_channel_media(channel_id=CHANNEL)

        assert report.no_url == 1
        download.assert_not_called()

    async def test_telegram_url_skipped(self, monkeypatch):
        docs = [_make_doc(1, [{"url": TELEGRAM_URL, "name": "a.jpg"}])]
        _install_stores(monkeypatch, docs=docs)
        download = _install_download(monkeypatch, returns=SAMPLE)

        report = await backfill_channel_media(channel_id=CHANNEL)

        assert report.skipped_telegram == 1
        download.assert_not_called()

    async def test_download_failure_counted_and_continues(self, monkeypatch):
        docs = [
            _make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}]),
            _make_doc(2, [{"url": DISCORD_URL, "name": "b.png"}]),
        ]
        stores, _messages, _cols = _install_stores(monkeypatch, docs=docs)
        _install_download(monkeypatch, returns=None)  # every download fails

        report = await backfill_channel_media(channel_id=CHANNEL)

        assert report.download_failed == 2  # both failed, scan continued
        assert report.messages_scanned == 2
        stores.media_blob_store.save_blob.assert_not_called()

    async def test_oversize_counted_distinct_from_download_failed(self, monkeypatch):
        # A size-cap rejection surfaces as the OVERSIZE sentinel and must be
        # reported under too_large, NOT lumped into download_failed.
        docs = [_make_doc(1, [{"url": DISCORD_URL, "name": "big.bin"}])]
        stores, _messages, _cols = _install_stores(monkeypatch, docs=docs)
        _install_download(monkeypatch, returns=mb.OVERSIZE)

        report = await backfill_channel_media(channel_id=CHANNEL)

        assert report.too_large == 1
        assert report.download_failed == 0
        assert report.stored == 0
        assert report.by_platform[SOURCE]["too_large"] == 1
        # Oversize bytes never reach the store.
        stores.media_blob_store.save_blob.assert_not_called()

    async def test_oversize_sentinel_requested_from_downloader(self, monkeypatch):
        docs = [_make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}])]
        _install_stores(monkeypatch, docs=docs)
        download = _install_download(monkeypatch, returns=SAMPLE)

        await backfill_channel_media(channel_id=CHANNEL)

        # Backfill must opt into the oversize sentinel so it can distinguish a
        # size-cap drop from auth/network failures.
        assert download.await_args_list[0].kwargs["return_oversize_sentinel"] is True

    async def test_successful_download_stores_with_right_args(self, monkeypatch):
        att = {"url": DISCORD_URL, "name": "a.png", "mimetype": "image/png"}
        docs = [_make_doc(1, [att])]
        stores, _messages, _cols = _install_stores(monkeypatch, docs=docs)
        _install_download(monkeypatch, returns=SAMPLE)

        report = await backfill_channel_media(channel_id=CHANNEL)

        assert report.stored == 1
        stores.media_blob_store.save_blob.assert_awaited_once()
        kwargs = stores.media_blob_store.save_blob.call_args.kwargs
        assert kwargs["content"] == SAMPLE
        assert kwargs["channel_id"] == CHANNEL
        assert kwargs["source_id"] == SOURCE
        assert kwargs["message_id"] == "M1"
        assert kwargs["mime_type"] == "image/png"
        assert kwargs["filename"] == "a.png"
        assert kwargs["platform_url"] == DISCORD_URL

    async def test_resume_state_written_every_batch_and_resumes(self, monkeypatch):
        docs = [
            _make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}]),
            _make_doc(2, [{"url": DISCORD_URL, "name": "b.png"}]),
            _make_doc(3, [{"url": DISCORD_URL, "name": "c.png"}]),
        ]
        _stores, messages, cols = _install_stores(monkeypatch, docs=docs)
        _install_download(monkeypatch, returns=SAMPLE)

        report = await backfill_channel_media(channel_id=CHANNEL, batch_size=2)

        assert report.messages_scanned == 3
        # Two non-empty batches (2 + 1) → two resume-state writes, last cursor=3.
        assert cols["state"].update_one.await_count == 2
        last_set = cols["state"].update_one.await_args_list[-1].args[1]["$set"]
        assert last_set["last_processed_id"] == 3

    async def test_resume_skips_already_processed(self, monkeypatch):
        docs = [
            _make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}]),
            _make_doc(2, [{"url": DISCORD_URL, "name": "b.png"}]),
        ]
        _stores, messages, cols = _install_stores(monkeypatch, docs=docs)
        # Saved cursor at _id=1 → only doc 2 should be scanned on resume.
        cols["state"].find_one = AsyncMock(return_value={"last_processed_id": 1})
        _install_download(monkeypatch, returns=SAMPLE)

        report = await backfill_channel_media(channel_id=CHANNEL)

        assert report.messages_scanned == 1
        # First find carried the resume bound.
        assert messages.find_queries[0]["_id"] == {"$gt": 1}

    async def test_reset_clears_resume_state(self, monkeypatch):
        docs = [_make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}])]
        _stores, _messages, cols = _install_stores(monkeypatch, docs=docs)
        cols["state"].find_one = AsyncMock(return_value={"last_processed_id": 99})
        _install_download(monkeypatch, returns=SAMPLE)

        await backfill_channel_media(channel_id=CHANNEL, reset=True)

        cols["state"].delete_one.assert_awaited_once()

    async def test_connection_id_resolved_per_channel_and_passed(self, monkeypatch):
        conn = MagicMock()
        conn.id = CONN_ID
        conn.selected_channels = [CHANNEL]
        docs = [
            _make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}]),
            _make_doc(2, [{"url": DISCORD_URL, "name": "b.png"}]),
        ]
        stores, _messages, _cols = _install_stores(monkeypatch, docs=docs, connections=[conn])
        download = _install_download(monkeypatch, returns=SAMPLE)

        await backfill_channel_media(channel_id=CHANNEL, batch_size=10)

        # Both downloads carried the resolved connection_id.
        for call in download.await_args_list:
            assert call.kwargs["connection_id"] == CONN_ID
        # Resolution is cached — list_connections called once despite 2 messages.
        stores.platform.list_connections.assert_awaited_once()

    async def test_unresolved_connection_falls_back_to_none(self, monkeypatch):
        docs = [_make_doc(1, [{"url": DISCORD_URL, "name": "a.png"}])]
        # No connection claims the channel.
        _stores, _messages, _cols = _install_stores(monkeypatch, docs=docs, connections=[])
        download = _install_download(monkeypatch, returns=SAMPLE)

        await backfill_channel_media(channel_id=CHANNEL)

        assert download.await_args_list[0].kwargs["connection_id"] is None
