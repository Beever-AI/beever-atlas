"""Tests for trace_decision_history error handling and empty-entity cases."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_graph_mock():
    graph = AsyncMock()
    return graph


# ---------------------------------------------------------------------------
# Test 1: ConnectionError → {"result": [], "error": "graph_unavailable"}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_decision_history_connection_error_returns_graph_unavailable():
    """When the graph backend raises ConnectionError, trace_decision_history
    must return {"result": [], "error": "graph_unavailable"} so the agent
    knows the graph is down, not merely empty."""
    from beever_atlas.agents.tools.graph_tools import trace_decision_history

    graph = _make_graph_mock()
    graph.fuzzy_match_entities.side_effect = ConnectionError("Cannot connect to Neo4j")

    stores_mock = MagicMock()
    stores_mock.graph = graph

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        result = await trace_decision_history(channel_id="C123", topic="deployment")

    assert isinstance(result, dict), f"Expected dict, got {type(result)}: {result!r}"
    assert result.get("error") == "graph_unavailable"
    assert result.get("result") == []


@pytest.mark.asyncio
async def test_trace_decision_history_oserror_returns_graph_unavailable():
    """OSError (e.g. socket failure) is also treated as graph_unavailable."""
    from beever_atlas.agents.tools.graph_tools import trace_decision_history

    graph = _make_graph_mock()
    graph.fuzzy_match_entities.side_effect = OSError("Network unreachable")

    stores_mock = MagicMock()
    stores_mock.graph = graph

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        result = await trace_decision_history(channel_id="C456", topic="auth")

    assert isinstance(result, dict)
    assert result.get("error") == "graph_unavailable"


# ---------------------------------------------------------------------------
# Test 2: No matching entity → clean [] with no error key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_decision_history_no_match_returns_empty_list():
    """When fuzzy_match_entities returns [], trace_decision_history returns []
    without an error key — the graph is up, there's just no data."""
    from beever_atlas.agents.tools.graph_tools import trace_decision_history

    graph = _make_graph_mock()
    graph.fuzzy_match_entities.return_value = []

    stores_mock = MagicMock()
    stores_mock.graph = graph

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        result = await trace_decision_history(channel_id="C123", topic="nonexistent")

    assert result == []


@pytest.mark.asyncio
async def test_trace_decision_history_entity_not_found_returns_empty_list():
    """When find_entity_by_name returns None, result is [] (no error key)."""
    from beever_atlas.agents.tools.graph_tools import trace_decision_history

    graph = _make_graph_mock()
    graph.fuzzy_match_entities.return_value = [("SomeTopic", 0.9)]
    graph.find_entity_by_name.return_value = None

    stores_mock = MagicMock()
    stores_mock.graph = graph

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        result = await trace_decision_history(channel_id="C123", topic="SomeTopic")

    assert result == []


# ---------------------------------------------------------------------------
# Test 3: Generic exception → [] (not graph_unavailable)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_decision_history_generic_exception_returns_empty_list():
    """Non-connection exceptions fall through to the generic handler and
    return [], not the graph_unavailable dict."""
    from beever_atlas.agents.tools.graph_tools import trace_decision_history

    graph = _make_graph_mock()
    graph.fuzzy_match_entities.side_effect = ValueError("unexpected schema")

    stores_mock = MagicMock()
    stores_mock.graph = graph

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        result = await trace_decision_history(channel_id="C123", topic="broken")

    assert result == []
