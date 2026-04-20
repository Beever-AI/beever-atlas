"""Tests for Fix #8: MCP tools clamp numeric params to documented ranges.

Invokes each tool through the registered FastMCP wrapper and asserts that
the clamped value is what actually reaches the capability layer.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _get_tool_fn(mcp, name: str):
    for key, tool in mcp._local_provider._components.items():
        if key.startswith(f"tool:{name}@") or key == f"tool:{name}":
            return tool.fn
    raise KeyError(f"No tool named '{name}' found in registry")


def _req(principal_id: str = "mcp:testhash"):
    m = MagicMock()
    m.scope = {"state": {"mcp_principal_id": principal_id}}
    return m


def _ctx():
    c = MagicMock()
    c.info = AsyncMock()
    c.warning = AsyncMock()
    return c


# ---------------------------------------------------------------------------
# search_channel_facts — limit 1..50
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_limit,expected_limit",
    [
        (0, 1),
        (-5, 1),
        (10, 10),
        (50, 50),
        (51, 50),
        (999, 50),
    ],
)
async def test_search_channel_facts_limit_clamped(input_limit, expected_limit):
    from beever_atlas.api.mcp_server import build_mcp

    fake_search = AsyncMock(return_value=[])
    with (
        patch("fastmcp.server.dependencies.get_http_request", return_value=_req()),
        patch("beever_atlas.capabilities.memory.search_channel_facts", new=fake_search),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_channel_facts")
        await fn(channel_id="ch-a", query="hi", ctx=_ctx(), limit=input_limit)

    assert fake_search.await_args.kwargs["limit"] == expected_limit


# ---------------------------------------------------------------------------
# get_recent_activity — days 1..90, limit 1..50
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_days,expected_days",
    [(0, 1), (-1, 1), (7, 7), (90, 90), (91, 90), (500, 90)],
)
async def test_get_recent_activity_days_clamped(input_days, expected_days):
    from beever_atlas.api.mcp_server import build_mcp

    fake = AsyncMock(return_value=[])
    with (
        patch("fastmcp.server.dependencies.get_http_request", return_value=_req()),
        patch("beever_atlas.capabilities.memory.get_recent_activity", new=fake),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "get_recent_activity")
        await fn(channel_id="ch-a", ctx=_ctx(), days=input_days)

    assert fake.await_args.kwargs["days"] == expected_days


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_limit,expected_limit",
    [(0, 1), (20, 20), (100, 50)],
)
async def test_get_recent_activity_limit_clamped(input_limit, expected_limit):
    from beever_atlas.api.mcp_server import build_mcp

    fake = AsyncMock(return_value=[])
    with (
        patch("fastmcp.server.dependencies.get_http_request", return_value=_req()),
        patch("beever_atlas.capabilities.memory.get_recent_activity", new=fake),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "get_recent_activity")
        await fn(channel_id="ch-a", ctx=_ctx(), limit=input_limit)

    assert fake.await_args.kwargs["limit"] == expected_limit


# ---------------------------------------------------------------------------
# search_media_references — limit 1..20
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_limit,expected_limit",
    [(0, 1), (5, 5), (20, 20), (21, 20), (999, 20)],
)
async def test_search_media_references_limit_clamped(input_limit, expected_limit):
    from beever_atlas.api.mcp_server import build_mcp

    fake = AsyncMock(return_value=[])
    with (
        patch("fastmcp.server.dependencies.get_http_request", return_value=_req()),
        patch("beever_atlas.capabilities.memory.search_media_references", new=fake),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_media_references")
        await fn(channel_id="ch-a", query="hi", ctx=_ctx(), limit=input_limit)

    assert fake.await_args.kwargs["limit"] == expected_limit


# ---------------------------------------------------------------------------
# find_experts — limit 1..20
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_limit,expected_limit",
    [(0, 1), (10, 10), (20, 20), (21, 20), (999, 20)],
)
async def test_find_experts_limit_clamped(input_limit, expected_limit):
    from beever_atlas.api.mcp_server import build_mcp

    fake = AsyncMock(return_value=[])
    with (
        patch("fastmcp.server.dependencies.get_http_request", return_value=_req()),
        patch("beever_atlas.capabilities.graph.find_experts", new=fake),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "find_experts")
        await fn(channel_id="ch-a", topic="billing", ctx=_ctx(), limit=input_limit)

    assert fake.await_args.kwargs["limit"] == expected_limit


# ---------------------------------------------------------------------------
# search_relationships — hops 1..4
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "input_hops,expected_hops",
    [(-1, 1), (0, 1), (1, 1), (4, 4), (5, 4), (100, 4)],
)
async def test_search_relationships_hops_clamped(input_hops, expected_hops):
    from beever_atlas.api.mcp_server import build_mcp

    fake = AsyncMock(return_value={"nodes": [], "edges": []})
    with (
        patch("fastmcp.server.dependencies.get_http_request", return_value=_req()),
        patch("beever_atlas.capabilities.graph.search_relationships", new=fake),
    ):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_relationships")
        await fn(
            channel_id="ch-a",
            entities=["alice"],
            ctx=_ctx(),
            hops=input_hops,
        )

    assert fake.await_args.kwargs["hops"] == expected_hops
