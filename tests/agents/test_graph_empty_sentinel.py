"""Tests for empty-graph in-shape sentinel (Issue #3)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.agents.citations.registry import SourceRegistry, _current


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_empty_subgraph():
    sg = MagicMock()
    sg.nodes = []
    sg.edges = []
    return sg


def _make_graph_mock(*, subgraph=None):
    graph = AsyncMock()
    graph.fuzzy_match_entities.return_value = [("EntityA", 0.9)]
    entity = MagicMock()
    entity.id = "entity-1"
    entity.name = "EntityA"
    graph.find_entity_by_name.return_value = entity
    graph.get_neighbors.return_value = subgraph or _make_empty_subgraph()
    graph.list_relationships.return_value = []
    return graph


# ---------------------------------------------------------------------------
# (a) search_relationships returns sentinel when graph has no edges
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_relationships_empty_sentinel():
    from beever_atlas.agents.tools.graph_tools import search_relationships

    graph_mock = _make_graph_mock(subgraph=_make_empty_subgraph())
    stores_mock = MagicMock()
    stores_mock.graph = graph_mock

    with patch("beever_atlas.stores.get_stores", return_value=stores_mock):
        result = await search_relationships(channel_id="C123", entities=["EntityA"])

    assert isinstance(result, list), "result must be a list"
    assert len(result) == 1
    sentinel = result[0]
    assert sentinel.get("_empty") is True
    assert sentinel.get("reason") == "no_edges"
    assert "entity" in sentinel


# ---------------------------------------------------------------------------
# (b) citation decorator skips _empty items and does NOT register a source
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_decorator_skips_empty_item():
    from beever_atlas.agents.tools._citation_decorator import cite_tool_output

    @cite_tool_output(kind="graph_relationship")
    async def _fake_tool(channel_id: str, entities: list[str]) -> list[dict]:
        return [{"_empty": True, "entity": "nobody", "reason": "no_edges"}]

    registry = SourceRegistry()
    token = _current.set(registry)
    try:
        result = await _fake_tool(channel_id="C123", entities=["nobody"])
    finally:
        _current.reset(token)

    # Sentinel item must be returned untouched
    assert result == [{"_empty": True, "entity": "nobody", "reason": "no_edges"}]
    # No source should have been registered
    assert len(registry._sources) == 0
