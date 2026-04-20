"""Phase 3 tasks 3.1–3.3: unit tests for whoami, list_connections, list_channels.

All capability calls and principal extraction are mocked so tests run without
any database/store infrastructure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers to call tool functions directly (bypassing FastMCP DI machinery)
# ---------------------------------------------------------------------------


def _make_ctx(principal_id: str | None = "mcp:testhash") -> MagicMock:
    """Build a minimal mock Context that satisfies _get_principal_id."""
    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": principal_id}}
    ctx = MagicMock()
    ctx.info = AsyncMock()
    ctx.warning = AsyncMock()
    return ctx, request_mock


def _patch_http_request(principal_id: str | None):
    """Patch get_http_request() to return a fake request with the given principal."""
    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": principal_id}}
    return patch(
        "beever_atlas.api.mcp_server.get_http_request_in_tool",
        return_value=request_mock,
    )


# We need to directly import and call the underlying tool functions rather than
# going through the FastMCP machinery. The cleanest way is to call the
# registered tool's underlying function via the tool manager after build_mcp().


def _get_tool_fn(mcp, name: str):
    """Return the underlying async function for a registered tool.

    FastMCP 3.x stores tools in ``mcp._local_provider._components`` with keys
    like ``"tool:whoami@"`` (versioned). We search by name prefix.
    """
    for key, tool in mcp._local_provider._components.items():
        if key.startswith(f"tool:{name}@") or key == f"tool:{name}":
            return tool.fn
    raise KeyError(f"No tool named '{name}' found in registry")


# ---------------------------------------------------------------------------
# whoami (task 3.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_returns_principal_and_connections(monkeypatch):
    """whoami returns principal_id, connection_ids list, and server_version."""
    from beever_atlas.api.mcp_server import build_mcp

    fake_conns = [
        {
            "connection_id": "conn-1",
            "platform": "slack",
            "display_name": "WS1",
            "status": "connected",
            "last_synced_at": None,
            "selected_channel_count": 3,
            "source": "ui",
        },
        {
            "connection_id": "conn-2",
            "platform": "discord",
            "display_name": "DS1",
            "status": "connected",
            "last_synced_at": None,
            "selected_channel_count": 1,
            "source": "ui",
        },
    ]

    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": "mcp:abc123"}}

    ctx_mock = MagicMock()
    ctx_mock.info = AsyncMock()

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=request_mock,
        ),
        patch(
            "beever_atlas.capabilities.connections.list_connections",
            new=AsyncMock(return_value=fake_conns),
        ),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "whoami")
        result = await fn(ctx=ctx_mock)

    assert result["principal_id"] == "mcp:abc123"
    assert result["connections"] == ["conn-1", "conn-2"]
    assert "server_version" in result


@pytest.mark.asyncio
async def test_whoami_missing_principal_returns_auth_error(monkeypatch):
    """whoami returns authentication_missing when no principal in scope."""
    from beever_atlas.api.mcp_server import build_mcp

    request_mock = MagicMock()
    request_mock.scope = {"state": {}}  # no mcp_principal_id

    ctx_mock = MagicMock()
    ctx_mock.info = AsyncMock()
    ctx_mock.warning = AsyncMock()

    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=request_mock,
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "whoami")
        result = await fn(ctx=ctx_mock)

    assert result == {"error": "authentication_missing"}


@pytest.mark.asyncio
async def test_whoami_empty_connections(monkeypatch):
    """whoami with no connections returns empty list, not an error."""
    from beever_atlas.api.mcp_server import build_mcp

    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": "mcp:xyz"}}
    ctx_mock = MagicMock()
    ctx_mock.info = AsyncMock()

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=request_mock,
        ),
        patch(
            "beever_atlas.capabilities.connections.list_connections",
            new=AsyncMock(return_value=[]),
        ),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "whoami")
        result = await fn(ctx=ctx_mock)

    assert result["connections"] == []
    assert result["principal_id"] == "mcp:xyz"


# ---------------------------------------------------------------------------
# list_connections (task 3.2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_connections_returns_full_dicts(monkeypatch):
    """list_connections wraps the capability and returns {connections: [...]}."""
    from beever_atlas.api.mcp_server import build_mcp

    fake_conns = [
        {
            "connection_id": "c1",
            "platform": "slack",
            "display_name": "WS",
            "status": "connected",
            "last_synced_at": None,
            "selected_channel_count": 2,
            "source": "ui",
        },
    ]
    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": "mcp:testhash"}}
    ctx_mock = MagicMock()
    ctx_mock.info = AsyncMock()

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=request_mock,
        ),
        patch(
            "beever_atlas.capabilities.connections.list_connections",
            new=AsyncMock(return_value=fake_conns),
        ),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "list_connections")
        result = await fn(ctx=ctx_mock)

    assert result == {"connections": fake_conns}


@pytest.mark.asyncio
async def test_list_connections_empty(monkeypatch):
    """list_connections with zero connections returns {connections: []}."""
    from beever_atlas.api.mcp_server import build_mcp

    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": "mcp:testhash"}}
    ctx_mock = MagicMock()

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=request_mock,
        ),
        patch(
            "beever_atlas.capabilities.connections.list_connections",
            new=AsyncMock(return_value=[]),
        ),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "list_connections")
        result = await fn(ctx=ctx_mock)

    assert result == {"connections": []}


# ---------------------------------------------------------------------------
# list_channels (task 3.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_channels_returns_channel_dicts(monkeypatch):
    """list_channels wraps the capability for an owned connection."""
    from beever_atlas.api.mcp_server import build_mcp

    fake_channels = [
        {
            "channel_id": "ch-a",
            "name": "ch-a",
            "platform": "slack",
            "last_sync_ts": None,
            "sync_status": None,
            "message_count_estimate": None,
        },
        {
            "channel_id": "ch-b",
            "name": "ch-b",
            "platform": "slack",
            "last_sync_ts": None,
            "sync_status": None,
            "message_count_estimate": None,
        },
    ]
    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": "mcp:testhash"}}
    ctx_mock = MagicMock()

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=request_mock,
        ),
        patch(
            "beever_atlas.capabilities.connections.list_channels",
            new=AsyncMock(return_value=fake_channels),
        ),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "list_channels")
        result = await fn(connection_id="conn-owned", ctx=ctx_mock)

    assert result == {"channels": fake_channels}


@pytest.mark.asyncio
async def test_list_channels_access_denied_returns_structured_error(monkeypatch):
    """list_channels maps ConnectionAccessDenied to the structured error shape."""
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ConnectionAccessDenied

    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": "mcp:testhash"}}
    ctx_mock = MagicMock()

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=request_mock,
        ),
        patch(
            "beever_atlas.capabilities.connections.list_channels",
            new=AsyncMock(side_effect=ConnectionAccessDenied("conn-other")),
        ),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "list_channels")
        result = await fn(connection_id="conn-other", ctx=ctx_mock)

    assert result["error"] == "connection_access_denied"
    assert result["connection_id"] == "conn-other"


@pytest.mark.asyncio
async def test_list_channels_invalid_connection_id_returns_error(monkeypatch):
    """connection_id that fails the regex returns invalid_parameter immediately."""
    from beever_atlas.api.mcp_server import build_mcp

    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": "mcp:testhash"}}
    ctx_mock = MagicMock()

    with patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=request_mock,
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "list_channels")
        result = await fn(connection_id="../../etc/passwd", ctx=ctx_mock)

    assert result["error"] == "invalid_parameter"
    assert result["parameter"] == "connection_id"
