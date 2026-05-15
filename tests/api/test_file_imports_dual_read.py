"""Dual-read fallback tests for file-imported channels.

Covers PR-A.6.2 of the OSS pipeline + wiki redesign: when the
``READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES`` feature flag is ON and the
unified ``channel_messages`` collection has rows for a file-imported
channel, the messages tab serves data from the store. Otherwise (flag
OFF, or flag ON but the store is empty), the legacy ``imported_messages``
collection backs the read.

Mirrors the pattern from ``test_channel_messages_dual_read.py`` (PR-A.5).
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.models.platform_connection import PlatformConnection
from beever_atlas.server.app import app


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


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────


def _file_connection(channel_id: str = "file-channel-1") -> PlatformConnection:
    """Build a connected ``platform="file"`` connection that owns one channel."""
    return PlatformConnection(
        id="file-conn-1",
        platform="file",
        source="ui",
        display_name="File Imports",
        status="connected",
        selected_channels=[channel_id],
        encrypted_credentials=b"",
        credential_iv=b"",
        credential_tag=b"",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        owner_principal_id="legacy:shared",
    )


class _FakeImportedCursor:
    """Mimics ``stores.mongodb.db['imported_messages'].find().sort().limit()``."""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def sort(self, *_args: Any, **_kwargs: Any) -> "_FakeImportedCursor":
        return self

    def limit(self, _n: int) -> "_FakeImportedCursor":
        return self

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


def _imported_doc(message_id: str, content: str = "from imported_messages") -> dict[str, Any]:
    return {
        "channel_id": "file-channel-1",
        "message_id": message_id,
        "content": content,
        "author": "U_LEGACY",
        "author_name": "Legacy User",
        "author_image": "",
        "platform": "file",
        "channel_name": "Legacy File Channel",
        "timestamp": datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        "timestamp_iso": "2026-04-01T12:00:00+00:00",
        "thread_id": None,
        "attachments": [],
        "reactions": [],
        "reply_count": 0,
    }


def _store_row(message_id: str, content: str = "from channel_messages") -> dict[str, Any]:
    """A ``channel_messages`` row matching the PR-A.6.1 schema."""
    return {
        "channel_id": "file-channel-1",
        "message_id": message_id,
        "source_id": "file",
        "content": content,
        "author": "U_FROM_STORE",
        "author_name": "Store User",
        "author_image": None,
        "channel_name": "File Channel (store)",
        "timestamp": datetime(2026, 4, 1, 12, 0, tzinfo=UTC),
        "thread_id": None,
        "attachments": [],
        "reactions": [],
        "reply_count": 0,
        "raw_metadata": {"is_bot": False, "links": []},
        "extraction_status": "pending",
    }


def _wire_file_stores(mock_stores: MagicMock) -> MagicMock:
    """Replace ``platform.list_connections`` with a file-connection only fixture
    and stub the imported_messages dict-of-collections surface.

    The default ``mock_stores`` fixture wires a Slack connection that owns the
    MockAdapter channels — for these tests we want a single ``platform=file``
    connection so the ``is_file_channel`` branch in ``api/channels.py`` fires.
    """
    file_conn = _file_connection()
    mock_stores.platform.list_connections = AsyncMock(return_value=[file_conn])

    # Stub the dict-of-collections access used by the legacy fallback path.
    fake_imported = MagicMock()
    fake_imported.find = MagicMock(return_value=_FakeImportedCursor([]))
    fake_imported.count_documents = AsyncMock(return_value=0)
    db_dict = {"imported_messages": fake_imported}
    mock_stores.mongodb.db = MagicMock()
    mock_stores.mongodb.db.__getitem__ = lambda _self, key: db_dict[key]
    # Used by `_compute_total_count`.
    mock_stores.mongodb.get_channel_sync_state = AsyncMock(return_value=None)
    return fake_imported


@pytest.fixture
async def client(mock_stores: MagicMock):  # noqa: ARG001 — wiring only
    """Async client with the standard mock stores wired up."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: Flag OFF — legacy `imported_messages` serves the request
# ─────────────────────────────────────────────────────────────────────────────


class TestFlagOff:
    """``READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES=false`` keeps the legacy path."""

    async def test_flag_off_uses_imported_messages(
        self,
        client: AsyncClient,
        mock_stores: MagicMock,
        captured_channel_logs: pytest.LogCaptureFixture,
    ) -> None:
        caplog = captured_channel_logs
        fake_imported = _wire_file_stores(mock_stores)
        fake_imported.find.return_value = _FakeImportedCursor(
            [_imported_doc("m1"), _imported_doc("m2")]
        )
        fake_imported.count_documents = AsyncMock(return_value=2)
        # Wire a sentinel — store accessor must NOT be called when the flag is OFF.
        mock_stores.mongodb.get_channel_messages = AsyncMock(
            side_effect=AssertionError("channel_messages must not be read when the flag is OFF")
        )

        with patch.dict(
            os.environ,
            {"READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES": "false"},
        ):
            from beever_atlas.infra.config import get_settings

            get_settings.cache_clear()
            with caplog.at_level(logging.INFO, logger="beever_atlas.api.channels"):
                response = await client.get("/api/channels/file-channel-1/messages?limit=10")

        assert response.status_code == 200, response.text
        data = response.json()
        # Authors come from the legacy collection; never the store sentinel.
        assert len(data["messages"]) == 2
        assert all(m["author"] == "U_LEGACY" for m in data["messages"])
        mock_stores.mongodb.get_channel_messages.assert_not_called()
        # Structured log emitted with source="imported_messages".
        records = [r for r in caplog.records if r.getMessage() == "file_imports_read"]
        assert len(records) == 1
        assert getattr(records[0], "source", None) == "imported_messages"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: Flag ON, store populated — channel_messages serves the request
# ─────────────────────────────────────────────────────────────────────────────


class TestFlagOnPopulated:
    """Flag ON + populated store → response is served from ``channel_messages``."""

    async def test_flag_on_populated_serves_from_channel_messages(
        self,
        client: AsyncClient,
        mock_stores: MagicMock,
        captured_channel_logs: pytest.LogCaptureFixture,
    ) -> None:
        caplog = captured_channel_logs
        _wire_file_stores(mock_stores)
        rows = [_store_row("m1"), _store_row("m2")]
        mock_stores.mongodb.get_channel_messages = AsyncMock(return_value=rows)

        with patch.dict(
            os.environ,
            {"READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES": "true"},
        ):
            from beever_atlas.infra.config import get_settings

            get_settings.cache_clear()
            with caplog.at_level(logging.INFO, logger="beever_atlas.api.channels"):
                response = await client.get("/api/channels/file-channel-1/messages?limit=10")

        assert response.status_code == 200, response.text
        data = response.json()
        assert len(data["messages"]) == 2
        assert all(m["author"] == "U_FROM_STORE" for m in data["messages"])
        # The store accessor was called with source_id="file" (single-source
        # filter — file-imported channels never carry chat-platform rows).
        call = mock_stores.mongodb.get_channel_messages.await_args
        assert call.kwargs.get("source_id") == "file"
        # Structured log emitted with source="channel_messages".
        records = [r for r in caplog.records if r.getMessage() == "file_imports_read"]
        assert len(records) == 1
        assert getattr(records[0], "source", None) == "channel_messages"


# ─────────────────────────────────────────────────────────────────────────────
# Scenario: Flag ON, store empty — fall back to imported_messages
# ─────────────────────────────────────────────────────────────────────────────


class TestFlagOnEmptyStore:
    """Flag ON + empty store → fall back to ``imported_messages`` and log."""

    async def test_flag_on_empty_falls_back_to_imported_messages(
        self,
        client: AsyncClient,
        mock_stores: MagicMock,
        captured_channel_logs: pytest.LogCaptureFixture,
    ) -> None:
        caplog = captured_channel_logs
        fake_imported = _wire_file_stores(mock_stores)
        fake_imported.find.return_value = _FakeImportedCursor([_imported_doc("m1")])
        fake_imported.count_documents = AsyncMock(return_value=1)
        mock_stores.mongodb.get_channel_messages = AsyncMock(return_value=[])

        with patch.dict(
            os.environ,
            {"READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES": "true"},
        ):
            from beever_atlas.infra.config import get_settings

            get_settings.cache_clear()
            with caplog.at_level(logging.INFO, logger="beever_atlas.api.channels"):
                response = await client.get("/api/channels/file-channel-1/messages?limit=10")

        assert response.status_code == 200, response.text
        data = response.json()
        # Legacy collection served the request.
        assert len(data["messages"]) == 1
        assert data["messages"][0]["author"] == "U_LEGACY"
        mock_stores.mongodb.get_channel_messages.assert_awaited_once()
        # Structured fallback log emitted with reason="empty_store".
        fallback_records = [r for r in caplog.records if r.getMessage() == "file_imports_fallback"]
        assert len(fallback_records) == 1
        assert getattr(fallback_records[0], "reason", None) == "empty_store"
        assert getattr(fallback_records[0], "channel_id", None) == "file-channel-1"
        # And then the legacy read emitted its own structured log.
        read_records = [r for r in caplog.records if r.getMessage() == "file_imports_read"]
        assert len(read_records) == 1
        assert getattr(read_records[0], "source", None) == "imported_messages"
