"""Dual-read fallback tests for ``GET /api/channels/{channel_id}/messages``.

Covers PR-A.5 of the OSS pipeline + wiki redesign: when the
``READ_FROM_MESSAGE_STORE`` feature flag is ON, the endpoint reads from the
durable ``channel_messages`` collection and falls back to
``adapter.fetch_history`` when the store is empty OR a sync is currently
running. Scenarios match
``openspec/changes/oss-pipeline-and-wiki-redesign/specs/message-store/spec.md``
→ "Requirement: Dual-read fallback during migration".
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.models.persistence import SyncJob
from beever_atlas.server.app import app

# The ``beever_atlas`` package logger is configured with ``propagate=False``
# in ``server/app.py``, so ``caplog.at_level`` alone does not capture its
# records (caplog attaches to the root handler by default). The fixture
# below adds the caplog handler directly to the package logger so the
# structured ``channel_messages_read`` / ``channel_messages_fallback``
# events surface for assertion in tests.


@pytest.fixture
def captured_channel_logs(caplog: pytest.LogCaptureFixture):
    """Yield ``caplog`` after attaching its handler to the channels logger."""
    target = logging.getLogger("beever_atlas.api.channels")
    target.addHandler(caplog.handler)
    target.setLevel(logging.INFO)
    try:
        yield caplog
    finally:
        target.removeHandler(caplog.handler)


# ----- shared fixtures --------------------------------------------------------


@pytest.fixture
async def client(mock_stores):  # noqa: ARG001 — dependency wires the stores
    """Async client with the standard mock stores wired up.

    Individual tests override ``mock_stores.mongodb.get_channel_messages`` and
    ``mock_stores.mongodb.get_sync_status`` per scenario.
    """
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def use_mock_adapter():
    """Force ADAPTER_MOCK=true so the legacy adapter path returns fixture data.

    Mirrors the pattern in ``tests/test_channels.py`` — without this the
    adapter singleton may be cached from an earlier test that used a real
    bridge.
    """
    import beever_atlas.adapters as adapters_mod

    saved = adapters_mod._adapter
    adapters_mod._adapter = None
    with patch.dict(os.environ, {"ADAPTER_MOCK": "true"}):
        yield
    adapters_mod._adapter = saved


def _store_row(message_id: str, content: str = "from store") -> dict:
    """Build a ``channel_messages`` row dict matching PR-A.3's schema."""
    return {
        "channel_id": "C_MOCK_GENERAL",
        "message_id": message_id,
        "source_id": "slack",
        "content": content,
        "author": "U_FROM_STORE",
        "author_name": "Store User",
        "author_image": None,
        "timestamp": datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC),
        "thread_id": None,
        "attachments": [],
        "reactions": [],
        "reply_count": 0,
        "raw_metadata": {"is_bot": False, "links": []},
        "extraction_status": "done",
    }


# ----- Scenario: Flag OFF -----------------------------------------------------


class TestFlagOff:
    """When ``READ_FROM_MESSAGE_STORE`` is OFF the endpoint uses the adapter."""

    @pytest.mark.asyncio
    async def test_flag_off_uses_adapter(self, client: AsyncClient, mock_stores):
        # Wire a sentinel on the store accessor so we can prove it was NOT
        # called. If the production code touches it the AsyncMock side_effect
        # raises and the assertion below fails with a clear message.
        mock_stores.mongodb.get_channel_messages = AsyncMock(
            side_effect=AssertionError("store must not be read when flag is OFF"),
        )
        mock_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

        with patch.dict(os.environ, {"READ_FROM_MESSAGE_STORE": "false"}):
            # Force a fresh Settings read (lru_cache invalidation).
            from beever_atlas.infra.config import get_settings

            get_settings.cache_clear()
            response = await client.get("/api/channels/C_MOCK_GENERAL/messages?limit=2")

        assert response.status_code == 200
        data = response.json()
        # The MockAdapter fixtures return non-empty messages and authors are
        # NOT the synthetic "Store User" we'd see if the store path ran.
        assert len(data["messages"]) > 0
        assert all(m["author"] != "U_FROM_STORE" for m in data["messages"])
        mock_stores.mongodb.get_channel_messages.assert_not_called()


# ----- Scenario: Flag ON, store populated, no sync in progress ----------------


class TestFlagOnPopulated:
    """Flag ON + populated store + idle sync → response is served from store."""

    @pytest.mark.asyncio
    async def test_flag_on_populated_serves_from_store(
        self,
        client: AsyncClient,
        mock_stores,
        captured_channel_logs: pytest.LogCaptureFixture,
    ):
        caplog = captured_channel_logs
        rows = [_store_row("M1", "store msg 1"), _store_row("M2", "store msg 2")]
        mock_stores.mongodb.get_channel_messages = AsyncMock(return_value=rows)
        mock_stores.mongodb.get_sync_status = AsyncMock(
            return_value=SyncJob(channel_id="C_MOCK_GENERAL", status="completed")
        )

        with patch.dict(os.environ, {"READ_FROM_MESSAGE_STORE": "true"}):
            from beever_atlas.infra.config import get_settings

            get_settings.cache_clear()
            with caplog.at_level(logging.INFO, logger="beever_atlas.api.channels"):
                response = await client.get("/api/channels/C_MOCK_GENERAL/messages?limit=10")

        assert response.status_code == 200
        data = response.json()
        # Authors come from the synthetic store rows, not from the adapter
        # fixtures — proves the store branch served this request.
        assert len(data["messages"]) == 2
        assert all(m["author"] == "U_FROM_STORE" for m in data["messages"])
        mock_stores.mongodb.get_channel_messages.assert_awaited_once()
        # Structured log emitted with the channel_messages_read event.
        # The handler captures `msg=` directly; the `extra=` dict is set as
        # attributes on the LogRecord but pytest's caplog inspects `.message`
        # for the rendered text, so we match against the message string (which
        # is itself the event name in this codebase's structured pattern).
        assert any(rec.getMessage() == "channel_messages_read" for rec in caplog.records)


# ----- Scenario: Flag ON, store empty -----------------------------------------


class TestFlagOnEmptyStore:
    """Empty ``channel_messages`` → fall back to adapter and log the reason."""

    @pytest.mark.asyncio
    async def test_flag_on_empty_falls_back_to_adapter(
        self,
        client: AsyncClient,
        mock_stores,
        captured_channel_logs: pytest.LogCaptureFixture,
    ):
        caplog = captured_channel_logs
        mock_stores.mongodb.get_channel_messages = AsyncMock(return_value=[])
        mock_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

        with patch.dict(os.environ, {"READ_FROM_MESSAGE_STORE": "true"}):
            from beever_atlas.infra.config import get_settings

            get_settings.cache_clear()
            with caplog.at_level(logging.INFO, logger="beever_atlas.api.channels"):
                response = await client.get("/api/channels/C_MOCK_GENERAL/messages?limit=2")

        assert response.status_code == 200
        data = response.json()
        # Adapter served the request — non-empty MockAdapter fixtures, none
        # of which carry the synthetic store-row author.
        assert len(data["messages"]) > 0
        assert all(m["author"] != "U_FROM_STORE" for m in data["messages"])
        # Both store and adapter were consulted in that order.
        mock_stores.mongodb.get_channel_messages.assert_awaited_once()
        # Structured fallback log emitted with reason="empty_store".
        fallback_records = [
            rec for rec in caplog.records if rec.getMessage() == "channel_messages_fallback"
        ]
        assert len(fallback_records) == 1
        # Standard logging copies `extra={...}` keys onto the LogRecord as
        # attributes — assert reason + channel_id were threaded through.
        assert getattr(fallback_records[0], "reason", None) == "empty_store"
        assert getattr(fallback_records[0], "channel_id", None) == "C_MOCK_GENERAL"


# ----- Scenario: Flag ON, sync in progress ------------------------------------


class TestFlagOnSyncInProgress:
    """Sync running for the channel → fall back even if store has rows."""

    @pytest.mark.asyncio
    async def test_flag_on_sync_running_falls_back_to_adapter(
        self,
        client: AsyncClient,
        mock_stores,
        captured_channel_logs: pytest.LogCaptureFixture,
    ):
        caplog = captured_channel_logs
        # Store has rows — but sync is mid-flight, so we MUST fall back to
        # avoid surfacing partial data.
        mock_stores.mongodb.get_channel_messages = AsyncMock(return_value=[_store_row("M1")])
        mock_stores.mongodb.get_sync_status = AsyncMock(
            return_value=SyncJob(channel_id="C_MOCK_GENERAL", status="running")
        )

        with patch.dict(os.environ, {"READ_FROM_MESSAGE_STORE": "true"}):
            from beever_atlas.infra.config import get_settings

            get_settings.cache_clear()
            with caplog.at_level(logging.INFO, logger="beever_atlas.api.channels"):
                response = await client.get("/api/channels/C_MOCK_GENERAL/messages?limit=2")

        assert response.status_code == 200
        data = response.json()
        # Adapter served — none of the store-row authors appear.
        assert len(data["messages"]) > 0
        assert all(m["author"] != "U_FROM_STORE" for m in data["messages"])
        # Structured fallback log emitted with reason="sync_in_progress".
        fallback_records = [
            rec for rec in caplog.records if rec.getMessage() == "channel_messages_fallback"
        ]
        assert len(fallback_records) == 1
        assert getattr(fallback_records[0], "reason", None) == "sync_in_progress"
        assert getattr(fallback_records[0], "channel_id", None) == "C_MOCK_GENERAL"
