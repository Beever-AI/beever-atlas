"""Tests for multi-workspace connection support.

Covers:
- Multiple connections per platform (model, store)
- Connection ID in adapter lifecycle
- display_name validation
- Env migration scoped to platform
- ChatBridgeAdapter connection-scoped routing
"""

from __future__ import annotations

import secrets
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from beever_atlas.models.platform_connection import PlatformConnection

_VALID_KEY_HEX = secrets.token_hex(32)


def _patch_key(monkeypatch) -> None:
    from beever_atlas.infra import config

    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", _VALID_KEY_HEX)
    config.get_settings.cache_clear()


def _make_store(monkeypatch):
    from beever_atlas.stores.platform_store import PlatformStore

    mock_col = MagicMock()
    return PlatformStore(mock_col)


def _minimal_conn(**overrides) -> PlatformConnection:
    defaults = dict(
        platform="slack",
        display_name="Test Workspace",
        encrypted_credentials=b"ciphertext",
        credential_iv=b"iv_12_bytes_",
        credential_tag=b"tag_16_bytes____",
    )
    defaults.update(overrides)
    return PlatformConnection(**defaults)


# ── Model tests ─────────────────────────────────────────────────────────


class TestMultipleConnectionsModel:
    """Test that PlatformConnection model supports multi-connection scenarios."""

    def test_two_slack_connections_get_different_ids(self):
        conn1 = _minimal_conn(platform="slack", display_name="Engineering")
        conn2 = _minimal_conn(platform="slack", display_name="Marketing")

        assert conn1.id != conn2.id
        assert conn1.platform == conn2.platform == "slack"
        assert conn1.display_name != conn2.display_name

    def test_explicit_connection_id_preserved(self):
        custom_id = "my-custom-id-123"
        conn = _minimal_conn(id=custom_id)

        assert conn.id == custom_id

    def test_all_platforms_accepted(self):
        for platform in ("slack", "discord", "teams", "telegram"):
            conn = _minimal_conn(platform=platform)
            assert conn.platform == platform

    def test_invalid_platform_rejected(self):
        with pytest.raises(ValidationError):
            _minimal_conn(platform="whatsapp")


# ── Store tests ──────────────────────────────────────────────────────────


class TestStoreMultiConnection:
    """Test PlatformStore methods for multi-connection support."""

    def test_store_serialization_round_trip_with_custom_id(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)
        conn = _minimal_conn(id="custom-conn-id")

        doc = store._to_doc(conn)
        result = store._from_doc(doc)

        assert result.id == "custom-conn-id"

    @pytest.mark.asyncio
    async def test_get_connections_by_platform_and_source(self, monkeypatch):
        _patch_key(monkeypatch)
        store = _make_store(monkeypatch)

        env_slack = _minimal_conn(platform="slack", source="env", display_name="Slack (env)")
        _minimal_conn(platform="slack", source="ui", display_name="My Slack")

        # Mock the find cursor
        async def mock_find_iter(query):
            docs = []
            if query.get("platform") == "slack" and query.get("source") == "env":
                docs = [store._to_doc(env_slack)]
            yield_docs = docs
            for d in yield_docs:
                yield d

        store._col.find = lambda query: mock_find_iter(query)

        result = await store.get_connections_by_platform_and_source("slack", "env")

        assert len(result) == 1
        assert result[0].source == "env"
        assert result[0].platform == "slack"


# ── ChatBridgeAdapter tests ──────────────────────────────────────────────


class TestChatBridgeAdapterConnectionScoped:
    """Test that ChatBridgeAdapter routes through connection-scoped endpoints."""

    def test_channel_path_with_connection_id(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.adapters.bridge import ChatBridgeAdapter

        adapter = ChatBridgeAdapter(
            bridge_url="http://localhost:3001",
            connection_id="conn-123",
        )

        path = adapter._channel_path("C001")
        assert path == "/bridge/connections/conn-123/channels/C001"

    def test_channel_path_without_connection_id(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.adapters.bridge import ChatBridgeAdapter

        adapter = ChatBridgeAdapter(bridge_url="http://localhost:3001")

        path = adapter._channel_path("C001")
        assert path == "/bridge/channels/C001"

    def test_channels_path_with_connection_id(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.adapters.bridge import ChatBridgeAdapter

        adapter = ChatBridgeAdapter(
            bridge_url="http://localhost:3001",
            connection_id="conn-456",
        )

        path = adapter._channels_path()
        assert path == "/bridge/connections/conn-456/channels"

    def test_channels_path_without_connection_id(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.adapters.bridge import ChatBridgeAdapter

        adapter = ChatBridgeAdapter(bridge_url="http://localhost:3001")

        path = adapter._channels_path()
        assert path == "/bridge/channels"


# ── API helper tests ──────────────────────────────────────────────────────


class TestAPIHelpers:
    """Test that API helper functions pass connection_id correctly."""

    @pytest.mark.asyncio
    async def test_register_adapter_sends_connection_id(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.api.connections import _register_adapter

        captured_json = {}

        class MockResponse:
            status_code = 200

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def post(self, path, json=None):
                captured_json.update(json or {})
                return MockResponse()

        monkeypatch.setattr(
            "beever_atlas.api.connections._bridge_client",
            lambda: MockClient(),
        )

        await _register_adapter("slack", {"token": "test"}, connection_id="abc-123")

        assert captured_json["connectionId"] == "abc-123"
        assert captured_json["platform"] == "slack"

    @pytest.mark.asyncio
    async def test_unregister_adapter_uses_connection_id(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.api.connections import _unregister_adapter

        captured_path = []

        class MockResponse:
            status_code = 200

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def delete(self, path):
                captured_path.append(path)
                return MockResponse()

        monkeypatch.setattr(
            "beever_atlas.api.connections._bridge_client",
            lambda: MockClient(),
        )

        await _unregister_adapter("conn-xyz")

        assert captured_path[0] == "/bridge/adapters/conn-xyz"

    @pytest.mark.asyncio
    async def test_list_bridge_channels_connection_scoped(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.api.connections import _list_bridge_channels

        captured_path = []

        class MockResponse:
            status_code = 200

            def json(self):
                return {"channels": [{"channel_id": "C001", "name": "general"}]}

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, path):
                captured_path.append(path)
                return MockResponse()

        monkeypatch.setattr(
            "beever_atlas.api.connections._bridge_client",
            lambda: MockClient(),
        )

        result = await _list_bridge_channels("slack", connection_id="conn-123")

        assert captured_path[0] == "/bridge/connections/conn-123/channels"
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_list_bridge_channels_legacy_fallback(self, monkeypatch):
        _patch_key(monkeypatch)
        from beever_atlas.api.connections import _list_bridge_channels

        captured_path = []

        class MockResponse:
            status_code = 200

            def json(self):
                return {"channels": []}

        class MockClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                pass

            async def get(self, path):
                captured_path.append(path)
                return MockResponse()

        monkeypatch.setattr(
            "beever_atlas.api.connections._bridge_client",
            lambda: MockClient(),
        )

        await _list_bridge_channels("slack")

        assert captured_path[0] == "/bridge/platforms/slack/channels"
