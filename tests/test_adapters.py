"""Tests for adapter layer: NormalizedMessage, ChatBridgeAdapter, MockAdapter, get_adapter."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from beever_atlas.adapters.base import (
    ChannelInfo,
    NormalizedMessage,
)
from beever_atlas.adapters.bridge import BridgeError, ChatBridgeAdapter
from beever_atlas.adapters.mock import MockAdapter


# ── NormalizedMessage tests ───────────────────────────────────────────────────


class TestNormalizedMessage:
    def test_create_basic(self):
        msg = NormalizedMessage(
            content="hello world",
            author="U123",
            platform="slack",
            channel_id="C123",
            channel_name="general",
            message_id="1234567890.123456",
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        assert msg.content == "hello world"
        assert msg.platform == "slack"
        assert msg.thread_id is None
        assert msg.attachments == []
        assert msg.reactions == []
        assert msg.reply_count == 0

    def test_create_with_thread(self):
        msg = NormalizedMessage(
            content="reply",
            author="U456",
            platform="slack",
            channel_id="C123",
            channel_name="general",
            message_id="1234567890.654321",
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            thread_id="1234567890.123456",
            reply_count=5,
        )
        assert msg.thread_id == "1234567890.123456"
        assert msg.reply_count == 5

    def test_create_with_reactions_and_attachments(self):
        msg = NormalizedMessage(
            content="check this out",
            author="U789",
            platform="slack",
            channel_id="C123",
            channel_name="general",
            message_id="ts1",
            timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
            reactions=[{"name": "thumbsup", "count": 3}],
            attachments=[{"id": "F1", "title": "doc.pdf"}],
        )
        assert len(msg.reactions) == 1
        assert msg.reactions[0]["name"] == "thumbsup"
        assert len(msg.attachments) == 1


# ── ChannelInfo tests ─────────────────────────────────────────────────────────


class TestChannelInfo:
    def test_create(self):
        info = ChannelInfo(
            channel_id="C123",
            name="general",
            platform="slack",
            member_count=50,
            topic="Team channel",
            purpose="General discussion",
        )
        assert info.channel_id == "C123"
        assert info.member_count == 50

    def test_optional_fields(self):
        info = ChannelInfo(channel_id="C123", name="test", platform="slack")
        assert info.member_count is None
        assert info.topic is None
        assert info.purpose is None


# ── ChatBridgeAdapter tests ──────────────────────────────────────────────────


class TestChatBridgeAdapter:
    def test_default_bridge_url(self):
        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("BRIDGE_URL", None)
            os.environ.pop("BRIDGE_API_KEY", None)
            adapter = ChatBridgeAdapter()
            assert adapter._bridge_url == "http://localhost:3001"

    def test_custom_bridge_url(self):
        adapter = ChatBridgeAdapter(bridge_url="http://bot:3001", api_key="test-key")
        assert adapter._bridge_url == "http://bot:3001"
        assert adapter._api_key == "test-key"

    def test_auth_header_set(self):
        adapter = ChatBridgeAdapter(bridge_url="http://bot:3001", api_key="my-secret")
        assert adapter._client.headers["authorization"] == "Bearer my-secret"

    def test_no_auth_header_when_no_key(self, monkeypatch):
        from beever_atlas.infra.config import get_settings
        settings = get_settings()
        monkeypatch.setattr(settings, "bridge_api_key", "")
        adapter = ChatBridgeAdapter(bridge_url="http://bot:3001", api_key="")
        assert "authorization" not in adapter._client.headers

    def test_normalize_message(self):
        adapter = ChatBridgeAdapter(bridge_url="http://test:3001")
        raw = {
            "content": "hello world",
            "author": "U123",
            "platform": "slack",
            "channel_id": "C123",
            "channel_name": "general",
            "message_id": "msg1",
            "timestamp": "2024-01-01T00:00:00+00:00",
            "thread_id": None,
            "attachments": [],
            "reactions": [{"name": "thumbsup", "count": 2}],
            "reply_count": 3,
        }
        msg = adapter.normalize_message(raw)
        assert msg.content == "hello world"
        assert msg.author == "U123"
        assert msg.platform == "slack"
        assert msg.channel_id == "C123"
        assert msg.reply_count == 3
        assert len(msg.reactions) == 1

    @pytest.mark.asyncio
    async def test_fetch_history(self):
        adapter = ChatBridgeAdapter(bridge_url="http://test:3001")
        mock_response = httpx.Response(
            200,
            json={
                "messages": [
                    {
                        "content": "msg1",
                        "author": "U1",
                        "platform": "slack",
                        "channel_id": "C123",
                        "channel_name": "general",
                        "message_id": "m1",
                        "timestamp": "2024-01-01T00:00:00+00:00",
                        "thread_id": None,
                        "attachments": [],
                        "reactions": [],
                        "reply_count": 0,
                    },
                    {
                        "content": "msg2",
                        "author": "U2",
                        "platform": "slack",
                        "channel_id": "C123",
                        "channel_name": "general",
                        "message_id": "m2",
                        "timestamp": "2024-01-01T01:00:00+00:00",
                        "thread_id": None,
                        "attachments": [],
                        "reactions": [],
                        "reply_count": 0,
                    },
                ]
            },
        )
        adapter._client.request = AsyncMock(return_value=mock_response)

        messages = await adapter.fetch_history("C123", limit=10)
        assert len(messages) == 2
        assert messages[0].content == "msg1"
        assert messages[1].content == "msg2"

    @pytest.mark.asyncio
    async def test_list_channels(self):
        adapter = ChatBridgeAdapter(bridge_url="http://test:3001")
        mock_response = httpx.Response(
            200,
            json={
                "channels": [
                    {"channel_id": "C1", "name": "general", "platform": "slack", "member_count": 10, "topic": None, "purpose": None},
                    {"channel_id": "C2", "name": "random", "platform": "slack", "member_count": 5, "topic": None, "purpose": None},
                ]
            },
        )
        adapter._client.request = AsyncMock(return_value=mock_response)

        channels = await adapter.list_channels()
        assert len(channels) == 2
        assert channels[0].name == "general"
        assert channels[1].name == "random"

    @pytest.mark.asyncio
    async def test_get_channel_info(self):
        adapter = ChatBridgeAdapter(bridge_url="http://test:3001")
        mock_response = httpx.Response(
            200,
            json={"channel_id": "C123", "name": "general", "platform": "slack", "member_count": 10, "topic": "Team", "purpose": "General"},
        )
        adapter._client.request = AsyncMock(return_value=mock_response)

        info = await adapter.get_channel_info("C123")
        assert info.name == "general"
        assert info.member_count == 10

    @pytest.mark.asyncio
    async def test_bridge_404_raises_key_error(self):
        adapter = ChatBridgeAdapter(bridge_url="http://test:3001")
        mock_response = httpx.Response(
            404,
            json={"error": "Channel NONEXISTENT not found", "code": "NOT_FOUND"},
        )
        adapter._client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(KeyError, match="NONEXISTENT"):
            await adapter.get_channel_info("NONEXISTENT")

    @pytest.mark.asyncio
    async def test_bridge_500_raises_bridge_error(self):
        adapter = ChatBridgeAdapter(bridge_url="http://test:3001")
        mock_response = httpx.Response(
            400,
            json={"error": "Bad request", "code": "BAD_REQUEST"},
        )
        adapter._client.request = AsyncMock(return_value=mock_response)

        with pytest.raises(BridgeError, match="Bad request"):
            await adapter.list_channels()


# ── MockAdapter tests ─────────────────────────────────────────────────────────


class TestMockAdapter:
    def test_loads_fixtures(self):
        adapter = MockAdapter()
        assert len(adapter._data.get("channels", [])) >= 2
        assert len(adapter._data.get("users", {})) >= 6

    @pytest.mark.asyncio
    async def test_list_channels(self):
        adapter = MockAdapter()
        channels = await adapter.list_channels()
        assert len(channels) >= 2
        names = [c.name for c in channels]
        assert "general" in names
        assert "engineering" in names

    @pytest.mark.asyncio
    async def test_get_channel_info(self):
        adapter = MockAdapter()
        info = await adapter.get_channel_info("C_MOCK_GENERAL")
        assert info.name == "general"
        assert info.platform == "slack"
        assert info.member_count == 8

    @pytest.mark.asyncio
    async def test_get_channel_info_not_found(self):
        adapter = MockAdapter()
        with pytest.raises(KeyError, match="NONEXISTENT"):
            await adapter.get_channel_info("NONEXISTENT")

    @pytest.mark.asyncio
    async def test_fetch_history(self):
        adapter = MockAdapter()
        messages = await adapter.fetch_history(
            "C_MOCK_GENERAL", limit=100, order="asc"
        )
        assert len(messages) > 0
        for i in range(1, len(messages)):
            assert messages[i].timestamp >= messages[i - 1].timestamp

    @pytest.mark.asyncio
    async def test_fetch_history_with_limit(self):
        adapter = MockAdapter()
        messages = await adapter.fetch_history("C_MOCK_GENERAL", limit=5)
        assert len(messages) == 5

    @pytest.mark.asyncio
    async def test_fetch_history_with_since(self):
        adapter = MockAdapter()
        since = datetime(2026, 3, 15, tzinfo=timezone.utc)
        messages = await adapter.fetch_history("C_MOCK_GENERAL", since=since)
        for msg in messages:
            assert msg.timestamp >= since

    @pytest.mark.asyncio
    async def test_fetch_thread(self):
        adapter = MockAdapter()
        messages = await adapter.fetch_thread("C_MOCK_GENERAL", "msg_g_002")
        assert len(messages) >= 3
        for msg in messages:
            assert msg.message_id == "msg_g_002" or msg.thread_id == "msg_g_002"

    @pytest.mark.asyncio
    async def test_multi_person_conversations(self):
        adapter = MockAdapter()
        messages = await adapter.fetch_history("C_MOCK_GENERAL", limit=100)
        authors = {m.author for m in messages}
        assert len(authors) >= 6

    @pytest.mark.asyncio
    async def test_messages_span_multiple_days(self):
        adapter = MockAdapter()
        messages = await adapter.fetch_history("C_MOCK_GENERAL", limit=100)
        if len(messages) >= 2:
            first_day = messages[0].timestamp.date()
            last_day = messages[-1].timestamp.date()
            span = abs((last_day - first_day).days)
            assert span >= 14

    @pytest.mark.asyncio
    async def test_decision_threads_exist(self):
        adapter = MockAdapter()
        messages = await adapter.fetch_history("C_MOCK_GENERAL", limit=100)
        decision_messages = [m for m in messages if "decision" in m.content.lower()]
        assert len(decision_messages) >= 2

    @pytest.mark.asyncio
    async def test_engineering_channel(self):
        adapter = MockAdapter()
        messages = await adapter.fetch_history("C_MOCK_ENGINEERING", limit=100)
        assert len(messages) > 0
        authors = {m.author for m in messages}
        assert len(authors) >= 3


# ── get_adapter factory tests ─────────────────────────────────────────────────


class TestGetAdapterFactory:
    def test_returns_mock_when_env_true(self):
        from beever_atlas.adapters import get_adapter

        with patch.dict(os.environ, {"ADAPTER_MOCK": "true"}):
            adapter = get_adapter()
            assert isinstance(adapter, MockAdapter)

    def test_returns_mock_when_env_1(self):
        from beever_atlas.adapters import get_adapter

        with patch.dict(os.environ, {"ADAPTER_MOCK": "1"}):
            adapter = get_adapter()
            assert isinstance(adapter, MockAdapter)

    def test_returns_bridge_when_no_mock_env(self):
        import beever_atlas.adapters as adapters_mod
        from beever_atlas.adapters import get_adapter

        env = os.environ.copy()
        env.pop("ADAPTER_MOCK", None)
        with patch.dict(os.environ, env, clear=True):
            adapters_mod._adapter = None
            adapter = get_adapter()
            assert isinstance(adapter, ChatBridgeAdapter)
        adapters_mod._adapter = None

    def test_bridge_adapter_is_default(self):
        import beever_atlas.adapters as adapters_mod
        from beever_atlas.adapters import get_adapter

        with patch.dict(os.environ, {}, clear=True):
            os.environ.pop("ADAPTER_MOCK", None)
            adapters_mod._adapter = None
            adapter = get_adapter()
            assert isinstance(adapter, ChatBridgeAdapter)
        adapters_mod._adapter = None
