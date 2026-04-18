"""Phase 3 task 3.5: unit tests for retrieval MCP tools.

Tests: ask_channel (stub), search_channel_facts, get_wiki_page,
       get_recent_activity, search_media_references.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _get_tool_fn(mcp, name: str):
    """Return the underlying async function for a registered tool.

    FastMCP 3.x stores tools in ``mcp._local_provider._components`` with keys
    like ``"tool:ask_channel@"`` (versioned). We search by name prefix.
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
    c.warning = AsyncMock()
    return c


# ---------------------------------------------------------------------------
# ask_channel stub (task 3.4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ask_channel_stub_returns_not_implemented():
    """ask_channel Phase 3 stub returns a structured not_implemented payload."""
    from beever_atlas.api.mcp_server import build_mcp

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.infra.channel_access.assert_channel_access",
               new=AsyncMock(return_value=None)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "ask_channel")
        result = await fn(channel_id="ch-a", question="Who owns the billing module?", ctx=_ctx())

    assert result["error"] == "not_implemented_in_phase3"


@pytest.mark.asyncio
async def test_ask_channel_access_denied_returns_structured_error():
    """ask_channel returns channel_access_denied when assert_channel_access raises 403."""
    from beever_atlas.api.mcp_server import build_mcp
    from fastapi import HTTPException

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.infra.channel_access.assert_channel_access",
               new=AsyncMock(side_effect=HTTPException(status_code=403))):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "ask_channel")
        result = await fn(channel_id="ch-x", question="anything", ctx=_ctx())

    assert result["error"] == "channel_access_denied"
    assert result["channel_id"] == "ch-x"


@pytest.mark.asyncio
async def test_ask_channel_missing_principal_returns_auth_error():
    req = MagicMock()
    req.scope = {"state": {}}  # no principal
    from beever_atlas.api.mcp_server import build_mcp

    with patch("fastmcp.server.dependencies.get_http_request", return_value=req):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "ask_channel")
        result = await fn(channel_id="ch-a", question="Q", ctx=_ctx())

    assert result["error"] == "authentication_missing"


@pytest.mark.asyncio
async def test_ask_channel_invalid_channel_id():
    from beever_atlas.api.mcp_server import build_mcp

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "ask_channel")
        result = await fn(channel_id="../../evil", question="Q", ctx=_ctx())

    assert result["error"] == "invalid_parameter"
    assert result["parameter"] == "channel_id"


# ---------------------------------------------------------------------------
# search_channel_facts (task 3.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_channel_facts_returns_dict_from_capability():
    from beever_atlas.api.mcp_server import build_mcp

    fake_facts = [{"text": "Billing is owned by Alice", "author": "alice",
                   "timestamp": "2024-01-01", "permalink": "https://example.com/1",
                   "channel_id": "ch-a"}]

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.memory.search_channel_facts",
               new=AsyncMock(return_value=fake_facts)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_channel_facts")
        result = await fn(channel_id="ch-a", query="billing", ctx=_ctx())

    assert result == {"facts": fake_facts}


@pytest.mark.asyncio
async def test_search_channel_facts_access_denied():
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.memory.search_channel_facts",
               new=AsyncMock(side_effect=ChannelAccessDenied("ch-x"))):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_channel_facts")
        result = await fn(channel_id="ch-x", query="billing", ctx=_ctx())

    assert result["error"] == "channel_access_denied"
    assert result["channel_id"] == "ch-x"


# ---------------------------------------------------------------------------
# get_wiki_page (task 3.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_wiki_page_returns_dict_from_capability():
    from beever_atlas.api.mcp_server import build_mcp

    fake_page = {"page_type": "overview", "channel_id": "ch-a",
                 "content": "This is the overview.", "summary": "Overview summary", "text": "Overview summary"}

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.wiki.get_wiki_page",
               new=AsyncMock(return_value=fake_page)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "get_wiki_page")
        result = await fn(channel_id="ch-a", ctx=_ctx())

    assert result["page_type"] == "overview"
    assert result["content"] == "This is the overview."


@pytest.mark.asyncio
async def test_get_wiki_page_access_denied():
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.wiki.get_wiki_page",
               new=AsyncMock(side_effect=ChannelAccessDenied("ch-x"))):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "get_wiki_page")
        result = await fn(channel_id="ch-x", ctx=_ctx())

    assert result["error"] == "channel_access_denied"
    assert result["channel_id"] == "ch-x"


# ---------------------------------------------------------------------------
# get_recent_activity (task 3.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_recent_activity_returns_dict():
    from beever_atlas.api.mcp_server import build_mcp

    fake_activity = [{"text": "Meeting notes", "author": "bob", "timestamp": "2024-01-15"}]

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.memory.get_recent_activity",
               new=AsyncMock(return_value=fake_activity)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "get_recent_activity")
        result = await fn(channel_id="ch-a", ctx=_ctx())

    assert result == {"activity": fake_activity}


@pytest.mark.asyncio
async def test_get_recent_activity_access_denied():
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.memory.get_recent_activity",
               new=AsyncMock(side_effect=ChannelAccessDenied("ch-x"))):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "get_recent_activity")
        result = await fn(channel_id="ch-x", ctx=_ctx())

    assert result["error"] == "channel_access_denied"


# ---------------------------------------------------------------------------
# search_media_references (task 3.5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_media_references_returns_dict():
    from beever_atlas.api.mcp_server import build_mcp

    fake_media = [{"text": "See attached diagram", "media_urls": ["https://img.example.com/a.png"]}]

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.memory.search_media_references",
               new=AsyncMock(return_value=fake_media)):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_media_references")
        result = await fn(channel_id="ch-a", query="diagram", ctx=_ctx())

    assert result == {"media": fake_media}


@pytest.mark.asyncio
async def test_search_media_references_access_denied():
    from beever_atlas.api.mcp_server import build_mcp
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req()), \
         patch("beever_atlas.capabilities.memory.search_media_references",
               new=AsyncMock(side_effect=ChannelAccessDenied("ch-x"))):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "search_media_references")
        result = await fn(channel_id="ch-x", query="diagram", ctx=_ctx())

    assert result["error"] == "channel_access_denied"
    assert result["channel_id"] == "ch-x"


# ---------------------------------------------------------------------------
# start_new_session (task 3.7)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_new_session_returns_session_id():
    from beever_atlas.api.mcp_server import build_mcp

    with patch("fastmcp.server.dependencies.get_http_request", return_value=_req("mcp:abc")):
        mcp = build_mcp()
        fn = _get_tool_fn(mcp, "start_new_session")
        result = await fn(ctx=_ctx())

    assert "session_id" in result
    session_id = result["session_id"]
    assert session_id.startswith("mcp:mcp:abc:")
    # The short uuid part should be 8 chars
    parts = session_id.rsplit(":", 1)
    assert len(parts[-1]) == 8
