"""Phase 3 task 3.6: unit tests for graph MCP tools.

Tests: find_experts, search_relationships, trace_decision_history.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _get_tool_fn(mcp, name: str):
    """Return the underlying async function for a registered tool.

    FastMCP 3.x stores tools in ``mcp._local_provider._components`` with keys
    like ``"tool:find_experts@"`` (versioned). We search by name prefix.
    """
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
    return c


# ---------------------------------------------------------------------------
# find_experts (task 3.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_experts_returns_dict_from_capability():
    from beever_atlas.api.mcp_server import build_mcp

    fake_experts = [
        {"handle": "alice", "expertise_score": 10.0, "fact_count": 10,
         "top_topics": ["billing"], "text": "alice has 10 facts about billing",
         "channel_id": "ch-a"},
    ]

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.graph.find_experts",
               new=AsyncMock(return_value=fake_experts)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "find_experts")
        result = await fn(channel_id="ch-a", topic="billing", ctx=_ctx())

    assert result == {"experts": fake_experts}


@pytest.mark.asyncio
async def test_find_experts_access_denied():
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.graph.find_experts",
               new=AsyncMock(side_effect=ChannelAccessDenied("ch-x"))):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "find_experts")
        result = await fn(channel_id="ch-x", topic="billing", ctx=_ctx())

    assert result["error"] == "channel_access_denied"
    assert result["channel_id"] == "ch-x"


@pytest.mark.asyncio
async def test_find_experts_invalid_channel_id():
    from beever_atlas.api.mcp_server import build_mcp

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "find_experts")
        result = await fn(channel_id="<script>", topic="billing", ctx=_ctx())

    assert result["error"] == "invalid_parameter"
    assert result["parameter"] == "channel_id"


# ---------------------------------------------------------------------------
# search_relationships (task 3.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_relationships_returns_dict_from_capability():
    from beever_atlas.api.mcp_server import build_mcp

    fake_result = {
        "entities_searched": ["BillingService"],
        "nodes": [{"name": "BillingService", "type": "service"}],
        "edges": [{"source": "BillingService", "target": "PaymentAPI",
                   "type": "CALLS", "confidence": 0.9, "context": ""}],
        "text": "BillingService -CALLS-> PaymentAPI",
        "channel_id": "ch-a",
    }

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.graph.search_relationships",
               new=AsyncMock(return_value=fake_result)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_relationships")
        result = await fn(channel_id="ch-a", entities=["BillingService"], ctx=_ctx())

    assert result["nodes"] == fake_result["nodes"]
    assert result["edges"] == fake_result["edges"]


@pytest.mark.asyncio
async def test_search_relationships_access_denied():
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.graph.search_relationships",
               new=AsyncMock(side_effect=ChannelAccessDenied("ch-x"))):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_relationships")
        result = await fn(channel_id="ch-x", entities=["X"], ctx=_ctx())

    assert result["error"] == "channel_access_denied"
    assert result["channel_id"] == "ch-x"


@pytest.mark.asyncio
async def test_search_relationships_list_result_wrapped():
    """If capability returns a list (empty-entity path), the tool wraps it."""
    from beever_atlas.api.mcp_server import build_mcp

    empty_list_result = [{"_empty": True, "entity": "X", "reason": "no_edges"}]

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.graph.search_relationships",
               new=AsyncMock(return_value=empty_list_result)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_relationships")
        result = await fn(channel_id="ch-a", entities=["X"], ctx=_ctx())

    # Tool wraps list result into {"edges": [...], "channel_id": ...}
    assert "edges" in result or "nodes" in result or "_empty" in result.get("edges", [{}])[0]


# ---------------------------------------------------------------------------
# trace_decision_history (task 3.6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_decision_history_returns_decisions():
    from beever_atlas.api.mcp_server import build_mcp

    fake_decisions = [
        {"entity": "old-api-v1", "superseded_by": "api-v2", "relationship": "SUPERSEDES",
         "confidence": 0.8, "context": "v2 replaces v1", "position": 0,
         "text": "v2 replaces v1", "channel_id": "ch-a", "topic": "API versioning"},
    ]

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.graph.trace_decision_history",
               new=AsyncMock(return_value=fake_decisions)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "trace_decision_history")
        result = await fn(channel_id="ch-a", topic="API versioning", ctx=_ctx())

    assert result == {"decisions": fake_decisions}


@pytest.mark.asyncio
async def test_trace_decision_history_access_denied():
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.graph.trace_decision_history",
               new=AsyncMock(side_effect=ChannelAccessDenied("ch-x"))):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "trace_decision_history")
        result = await fn(channel_id="ch-x", topic="something", ctx=_ctx())

    assert result["error"] == "channel_access_denied"
    assert result["channel_id"] == "ch-x"


@pytest.mark.asyncio
async def test_trace_decision_history_missing_principal():
    from beever_atlas.api.mcp_server import build_mcp

    req_no_principal = MagicMock()
    req_no_principal.scope = {"state": {}}

    with patch("fastmcp.server.dependencies.get_http_request", return_value=req_no_principal):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "trace_decision_history")
        result = await fn(channel_id="ch-a", topic="something", ctx=_ctx())

    assert result["error"] == "authentication_missing"
