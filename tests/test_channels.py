"""Tests for channels and messages API endpoints."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.server.app import app


@pytest.fixture
async def client(mock_stores):  # noqa: ARG001 — dependency wires the stores
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def use_mock_adapter():
    # Reset the adapter singleton so each test gets a fresh one honoring
    # ADAPTER_MOCK=true. Without this, the first real test run caches the
    # bridge adapter and later tests would not see the mock.
    import beever_atlas.adapters as adapters_mod

    saved = adapters_mod._adapter
    adapters_mod._adapter = None
    with patch.dict(os.environ, {"ADAPTER_MOCK": "true"}):
        yield
    adapters_mod._adapter = saved


class TestListChannels:
    @pytest.mark.asyncio
    async def test_returns_channels(self, client: AsyncClient):
        response = await client.get("/api/channels")
        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 2
        names = [ch["name"] for ch in data]
        assert "general" in names
        assert "engineering" in names

    @pytest.mark.asyncio
    async def test_channel_has_required_fields(self, client: AsyncClient):
        response = await client.get("/api/channels")
        data = response.json()
        ch = data[0]
        assert "channel_id" in ch
        assert "name" in ch
        assert "platform" in ch


class TestGetChannel:
    @pytest.mark.asyncio
    async def test_returns_channel_info(self, client: AsyncClient):
        response = await client.get("/api/channels/C_MOCK_GENERAL")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "general"
        assert data["platform"] == "slack"
        assert data["member_count"] == 8

    @pytest.mark.asyncio
    async def test_not_found_channel(self, client: AsyncClient):
        # RES-177 H1 (revised): in single-tenant mode (the test default)
        # a user principal may browse any channel id — `selected_channels`
        # is a sync pick-list, not an access ACL. The guard admits the
        # request and the adapter then returns 404 because no such channel
        # exists. In multi-tenant mode the guard would return 403.
        response = await client.get("/api/channels/NONEXISTENT")
        assert response.status_code == 404


class TestGetMessages:
    @pytest.mark.asyncio
    async def test_returns_messages(self, client: AsyncClient):
        response = await client.get("/api/channels/C_MOCK_GENERAL/messages")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) > 0
        assert "total_count" in data

    @pytest.mark.asyncio
    async def test_messages_have_required_fields(self, client: AsyncClient):
        response = await client.get("/api/channels/C_MOCK_GENERAL/messages?limit=1")
        data = response.json()
        msg = data["messages"][0]
        assert "content" in msg
        assert "author" in msg
        assert "platform" in msg
        assert "message_id" in msg
        assert "timestamp" in msg

    @pytest.mark.asyncio
    async def test_limit_parameter(self, client: AsyncClient):
        response = await client.get("/api/channels/C_MOCK_GENERAL/messages?limit=5")
        data = response.json()
        assert len(data["messages"]) == 5

    @pytest.mark.asyncio
    async def test_since_parameter(self, client: AsyncClient):
        response = await client.get(
            "/api/channels/C_MOCK_GENERAL/messages?since=2026-03-15T00:00:00Z"
        )
        assert response.status_code == 200
        data = response.json()
        for msg in data["messages"]:
            assert msg["timestamp"] >= "2026-03-15"

    @pytest.mark.asyncio
    async def test_engineering_channel(self, client: AsyncClient):
        response = await client.get("/api/channels/C_MOCK_ENGINEERING/messages?limit=10")
        assert response.status_code == 200
        data = response.json()
        assert len(data["messages"]) > 0
