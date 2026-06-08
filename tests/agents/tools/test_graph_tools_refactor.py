"""Post-refactor shape tests for graph_tools (Stream 3b).

Each test mocks the GraphStore, calls the tool, and asserts:
- The return shape matches the TypedDict (all required keys present,
  no extra keys).
- ``_src_id``-related citation-decorator fields survive the refactor.
- Empty-result sentinels remain list-shaped where the tool is
  list-returning, preserving the citation decorator's list branch.
"""

from __future__ import annotations

from typing import get_type_hints
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.agents.tools.graph_tools import (
    DecisionEvent,
    ExpertHit,
    RelationshipSearchResult,
    find_experts,
    search_relationships,
    trace_decision_history,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _node(name: str, type_: str = "Person", id_: str | None = None):
    n = MagicMock()
    n.name = name
    n.id = id_ or name
    n.type = type_
    n.entity_type = type_
    return n


def _edge(source: str, target: str, type_: str, confidence: float = 0.8, context: str = "ctx"):
    e = MagicMock()
    e.source = source
    e.target = target
    e.type = type_
    e.confidence = confidence
    e.context = context
    return e


def _subgraph(nodes, edges):
    sg = MagicMock()
    sg.nodes = nodes
    sg.edges = edges
    return sg


def _graph_mock():
    return AsyncMock()


def _patch_stores(graph):
    stores_mock = MagicMock()
    stores_mock.graph = graph
    return patch("beever_atlas.stores.get_stores", return_value=stores_mock)


def _required_keys(td_cls) -> set[str]:
    return set(get_type_hints(td_cls).keys())


# ---------------------------------------------------------------------------
# search_relationships
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_relationships_shape_matches_typeddict():
    graph = _graph_mock()
    graph.fuzzy_match_entities.return_value = [("Alice", 0.95)]
    graph.find_entity_by_name.return_value = _node("Alice")
    graph.get_neighbors.return_value = _subgraph(
        [_node("Alice"), _node("Bob", type_="Person")],
        [_edge("Alice", "Bob", "WORKS_WITH", 0.9)],
    )

    with _patch_stores(graph):
        result = await search_relationships("C1", ["Alice"], hops=2)

    assert isinstance(result, dict)
    assert set(result.keys()) == _required_keys(RelationshipSearchResult)
    # src_id / citation-decorator prerequisites
    assert result["subject_id"] == "Alice"
    assert result["predicate"] == "RELATED_TO"
    assert result["object_id"] == "C1"
    assert result["channel_id"] == "C1"
    assert result["text"]
    # Node/edge shapes
    for node in result["nodes"]:
        assert set(node.keys()) == {"name", "type"}
    for edge in result["edges"]:
        assert set(edge.keys()) == {"source", "target", "type", "confidence", "context"}


@pytest.mark.asyncio
async def test_search_relationships_caps_nodes_and_edges():
    graph = _graph_mock()
    graph.fuzzy_match_entities.return_value = [("Alice", 0.95)]
    graph.find_entity_by_name.return_value = _node("Alice")
    # 30 unique nodes, 80 unique edges of varying confidence.
    nodes = [_node(f"N{i}") for i in range(30)]
    edges = [_edge(f"N{i}", f"N{i + 1}", "REL", confidence=i / 100.0) for i in range(80)]
    graph.get_neighbors.return_value = _subgraph(nodes, edges)

    with _patch_stores(graph):
        result = await search_relationships("C1", ["Alice"])

    assert len(result["nodes"]) <= 20
    assert len(result["edges"]) <= 50
    # Highest-confidence edges retained
    confidences = [e["confidence"] for e in result["edges"]]
    assert confidences == sorted(confidences, reverse=True)


@pytest.mark.asyncio
async def test_search_relationships_empty_returns_list_sentinel():
    graph = _graph_mock()
    graph.fuzzy_match_entities.return_value = []

    with _patch_stores(graph):
        result = await search_relationships("C1", ["Nobody"])

    assert isinstance(result, list)
    assert result == [{"_empty": True, "entity": "Nobody", "reason": "no_edges"}]


# ---------------------------------------------------------------------------
# trace_decision_history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_decision_history_shape_matches_typeddict():
    graph = _graph_mock()
    graph.fuzzy_match_entities.return_value = [("ArchV1", 0.95)]
    graph.find_entity_by_name.return_value = _node("ArchV1")
    graph.get_neighbors.return_value = _subgraph(
        [_node("ArchV1"), _node("ArchV2")],
        [
            _edge("ArchV2", "ArchV1", "SUPERSEDES", 0.9, "v2 replaces v1"),
            _edge("ArchV3", "ArchV2", "SUPERSEDES", 0.85, "v3 replaces v2"),
            _edge("Alice", "ArchV1", "WORKS_ON", 0.5),  # non-SUPERSEDES ignored
        ],
    )

    with _patch_stores(graph):
        result = await trace_decision_history("C1", "ArchV1")

    assert isinstance(result, list)
    assert len(result) == 2
    required = _required_keys(DecisionEvent)
    for i, event in enumerate(result):
        assert set(event.keys()) == required
        assert event["relationship"] == "SUPERSEDES"
        assert event["position"] == i
        assert event["channel_id"] == "C1"
        assert event["topic"] == "ArchV1"
        # citation-decorator src field
        assert event["decision_id"].startswith("C1:")
        assert event["text"]


@pytest.mark.asyncio
async def test_trace_decision_history_empty_returns_list_sentinel():
    graph = _graph_mock()
    graph.fuzzy_match_entities.return_value = [("X", 0.9)]
    graph.find_entity_by_name.return_value = _node("X")
    graph.get_neighbors.return_value = _subgraph([_node("X")], [])

    with _patch_stores(graph):
        result = await trace_decision_history("C1", "X")

    assert isinstance(result, list)
    assert result == [{"_empty": True, "entity": "X", "reason": "no_edges"}]


# ---------------------------------------------------------------------------
# find_experts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_experts_shape_matches_typeddict():
    graph = _graph_mock()
    # Build relationships where "alice" co-occurs with "Database Topic".
    rels = [
        _edge("alice", "Database Migration", "WORKS_ON"),
        _edge("alice", "Database Schema", "DESIGNED"),
        _edge("bob", "Database Migration", "REVIEWED"),
    ]
    graph.list_relationships.return_value = rels

    with _patch_stores(graph):
        result = await find_experts("C1", "Database", limit=5)

    assert isinstance(result, list)
    assert len(result) >= 1
    required = _required_keys(ExpertHit)
    for hit in result:
        assert set(hit.keys()) == required
        assert hit["predicate"] == "EXPERT_IN"
        assert hit["object_id"] == "Database"
        assert hit["channel_id"] == "C1"
        assert hit["text"]
        assert isinstance(hit["top_topics"], list)
        assert isinstance(hit["recent_activity_days"], int)
    # Ordered by expertise_score desc
    scores = [h["expertise_score"] for h in result]
    assert scores == sorted(scores, reverse=True)


@pytest.mark.asyncio
async def test_find_experts_empty_returns_list_sentinel():
    graph = _graph_mock()
    graph.list_relationships.return_value = []
    graph.list_entities.return_value = []

    with (
        _patch_stores(graph),
        patch(
            "beever_atlas.capabilities.memory._search_channel_facts_impl",
            AsyncMock(return_value=[]),
        ),
    ):
        result = await find_experts("C1", "Nothing")

    assert isinstance(result, list)
    assert result == [{"_empty": True, "entity": "Nothing", "reason": "no_edges"}]


def _handles(result) -> set[str]:
    return {h.get("handle") or h.get("subject_id") for h in result if "_empty" not in h}


@pytest.mark.asyncio
async def test_find_experts_excludes_non_person_entities():
    """A concept on the other end of a topic-name match must NOT be ranked as
    an expert — only entities typed Person count. Guards the regression where
    'who contributes to project' returned 'Copyright-assignment CLA' / 'FSF'."""
    graph = _graph_mock()
    graph.list_entities.return_value = [_node("alice"), _node("bob")]
    graph.list_relationships.return_value = [
        _edge("alice", "Project Apollo", "WORKS_ON"),
        _edge("Copyright-assignment CLA", "Project Apollo", "RELATES_TO"),
    ]

    with (
        _patch_stores(graph),
        patch(
            "beever_atlas.capabilities.memory._search_channel_facts_impl",
            AsyncMock(return_value=[]),
        ),
    ):
        result = await find_experts("C1", "Project", limit=5)

    handles = _handles(result)
    assert "alice" in handles
    assert "Copyright-assignment CLA" not in handles


@pytest.mark.asyncio
async def test_find_experts_falls_back_to_fact_authorship():
    """When the entity graph yields no people, rank the humans who authored
    facts about the topic; skip system/bot authors."""
    graph = _graph_mock()
    graph.list_entities.return_value = []
    graph.list_relationships.return_value = []  # no graph signal

    facts = (
        [{"author": "Carol"}] * 3
        + [{"author": "Dave"}]
        + [{"author": "Beever Atlas"}] * 5  # bot — must be skipped
    )
    with (
        _patch_stores(graph),
        patch(
            "beever_atlas.capabilities.memory._search_channel_facts_impl",
            AsyncMock(return_value=facts),
        ),
    ):
        result = await find_experts("C1", "deployment", limit=5)

    handles = _handles(result)
    assert "Carol" in handles
    assert "Dave" in handles
    assert "Beever Atlas" not in handles
    # Carol (3 facts) must outrank Dave (1 fact).
    ordered = [h.get("handle") or h.get("subject_id") for h in result if "_empty" not in h]
    assert ordered.index("Carol") < ordered.index("Dave")


# ---------------------------------------------------------------------------
# Name freeze (defensive — Stream 1 also enforces this at the registry level)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trace_decision_history_falls_back_to_fact_timeline():
    """When the topic entity exists but has no SUPERSEDES chain, trace must
    return a chronological fact-based timeline (not the empty sentinel), so
    'trace the decisions around X' answers from real activity."""
    graph = _graph_mock()
    graph.fuzzy_match_entities.return_value = [("OSS launch", 0.9)]
    graph.find_entity_by_name.return_value = _node("OSS launch", type_="Topic")
    # Edges exist but NONE are SUPERSEDES.
    graph.get_neighbors.return_value = _subgraph(
        [_node("OSS launch")], [_edge("Alice", "OSS launch", "MENTIONED")]
    )
    facts = [
        {"text": "Plan approved", "message_ts": "200", "confidence": 0.9, "fact_id": "f2"},
        {"text": "Plan drafted", "message_ts": "100", "confidence": 0.8, "fact_id": "f1"},
    ]
    with (
        _patch_stores(graph),
        patch(
            "beever_atlas.capabilities.memory._search_channel_facts_impl",
            AsyncMock(return_value=facts),
        ),
    ):
        result = await trace_decision_history("C1", "OSS launch")

    assert isinstance(result, list)
    assert all("_empty" not in e for e in result)
    # Sorted oldest-first by message_ts.
    assert [e["text"] for e in result] == ["Plan drafted", "Plan approved"]
    assert result[0]["relationship"] == "EVENT"
    assert result[0]["topic"] == "OSS launch"


def test_public_tool_names_frozen():
    assert search_relationships.__name__ == "search_relationships"
    assert trace_decision_history.__name__ == "trace_decision_history"
    assert find_experts.__name__ == "find_experts"
