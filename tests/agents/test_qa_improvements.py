"""Unit tests for QA-agent improvement fixes.

Fix 1 — graph tools citation decorators (search_relationships, find_experts)
Fix 2 — MMR diversity re-rank in search_channel_facts
Fix 3 — stale "No activity recorded" sentinel fallback in get_wiki_page
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.agents.citations.registry import bind, reset


# ---------------------------------------------------------------------------
# Fix 1 — search_relationships and find_experts carry _cite / _src_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_relationships_has_cite_annotation():
    """search_relationships result dict should carry _cite and _src_id when
    the citation registry is bound."""
    from beever_atlas.agents.tools.graph_tools import search_relationships

    # Build mock subgraph objects
    mock_node = MagicMock()
    mock_node.name = "Node1"
    mock_node.entity_type = "Component"

    mock_edge = MagicMock()
    mock_edge.source = "Node1"
    mock_edge.target = "Node2"
    mock_edge.type = "DEPENDS_ON"
    mock_edge.confidence = 0.9
    mock_edge.context = "Node1 depends on Node2"

    mock_subgraph = MagicMock()
    mock_subgraph.nodes = [mock_node]
    mock_subgraph.edges = [mock_edge]

    mock_graph = MagicMock()
    mock_graph.fuzzy_match_entities = AsyncMock(return_value=[("Node1", 0.95)])
    mock_graph.find_entity_by_name = AsyncMock(return_value=MagicMock(id="ent-1", name="Node1"))
    mock_graph.get_neighbors = AsyncMock(return_value=mock_subgraph)

    mock_stores = MagicMock()
    mock_stores.graph = mock_graph

    r, tok = bind()
    try:
        with patch(
            "beever_atlas.stores.get_stores",
            return_value=mock_stores,
        ):
            result = await search_relationships(channel_id="C1", entities=["Node1"])
    finally:
        reset(tok)

    assert "_cite" in result, "search_relationships result missing _cite"
    assert "_src_id" in result, "search_relationships result missing _src_id"
    assert result["_cite"].startswith("[src:src_")


@pytest.mark.asyncio
async def test_find_experts_has_cite_annotation():
    """find_experts result items should carry _cite and _src_id when
    the citation registry is bound."""
    from beever_atlas.agents.tools.graph_tools import find_experts

    mock_rel = MagicMock()
    mock_rel.source = "alice"
    mock_rel.target = "authentication"

    mock_graph = MagicMock()
    mock_graph.list_relationships = AsyncMock(return_value=[mock_rel])

    mock_stores = MagicMock()
    mock_stores.graph = mock_graph

    r, tok = bind()
    try:
        with patch(
            "beever_atlas.stores.get_stores",
            return_value=mock_stores,
        ):
            results = await find_experts(channel_id="C1", topic="authentication", limit=5)
    finally:
        reset(tok)

    assert len(results) > 0, "find_experts returned no results"
    first = results[0]
    assert "_cite" in first, "find_experts item missing _cite"
    assert "_src_id" in first, "find_experts item missing _src_id"
    assert first["_cite"].startswith("[src:src_")


# ---------------------------------------------------------------------------
# Fix 2 — MMR re-rank returns a diverse mix
# ---------------------------------------------------------------------------


def test_mmr_rerank_returns_diverse_mix():
    """Feed 5 near-duplicate docs + 5 partially-relevant diverse docs;
    assert MMR selects a mix when λ is set to diversity-favouring value.

    Uses _mmr_rerank directly so no I/O is involved.
    """
    from beever_atlas.agents.tools.memory_tools import _mmr_rerank

    query_tokens = {"python", "deployment", "ci"}

    # Near-duplicates: all about "python deployment ci pipeline" (identical token sets)
    near_dupes = [
        {"text": "python deployment ci pipeline automated", "fact_id": f"dup-{i}"} for i in range(5)
    ]
    # Diverse docs: share at least one query token so they have non-zero relevance
    diverse = [
        {"text": "python database migration postgres schema", "fact_id": "div-0"},
        {"text": "ci build failure debugging logs analysis", "fact_id": "div-1"},
        {"text": "deployment rollback strategy production", "fact_id": "div-2"},
        {"text": "python security vulnerability cve patch", "fact_id": "div-3"},
        {"text": "ci deployment onboarding new developer", "fact_id": "div-4"},
    ]

    candidates = near_dupes + diverse
    # Use λ=0.4 (diversity-favouring) so that near-dupes penalise each other strongly
    selected = _mmr_rerank(candidates, query_tokens, k=5, lam=0.4)

    assert len(selected) == 5

    selected_ids = {d["fact_id"] for d in selected}
    dup_count = sum(1 for fid in selected_ids if fid.startswith("dup-"))
    div_count = sum(1 for fid in selected_ids if fid.startswith("div-"))

    # With λ=0.4 (diversity-heavy), near-dupes should penalise each other heavily
    # — expect at least 2 diverse docs in the final selection
    assert div_count >= 2, (
        f"MMR should select diverse docs but got dup_count={dup_count} div_count={div_count}"
    )


def test_mmr_rerank_respects_k():
    """_mmr_rerank never returns more than k items."""
    from beever_atlas.agents.tools.memory_tools import _mmr_rerank

    candidates = [{"text": f"fact {i}"} for i in range(20)]
    selected = _mmr_rerank(candidates, {"fact"}, k=7)
    assert len(selected) == 7


def test_mmr_rerank_empty_input():
    """_mmr_rerank handles empty candidate list gracefully."""
    from beever_atlas.agents.tools.memory_tools import _mmr_rerank

    assert _mmr_rerank([], set(), k=5) == []


# ---------------------------------------------------------------------------
# Fix 3 — stale "No activity recorded" sentinel triggers fresh fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_activity_page_returns_fresh_facts():
    """When the stored activity page contains the stale sentinel string,
    get_wiki_page should call get_recent_activity and return fresh content."""
    from beever_atlas.agents.tools.wiki_tools import get_wiki_page

    stale_page = {
        "content": "No activity recorded in the last 7 days",
        "summary": "",
    }

    fresh_facts = [
        {
            "text": "Alice shipped the auth refactor",
            "author": "alice",
            "timestamp": "2026-04-10",
        },
        {
            "text": "Bob merged the CI fix",
            "author": "bob",
            "timestamp": "2026-04-09",
        },
    ]

    mock_cache = MagicMock()
    mock_cache.get_page = AsyncMock(return_value=stale_page)

    mock_settings = MagicMock()
    mock_settings.mongodb_uri = "mongodb://mock"

    with (
        patch(
            "beever_atlas.infra.config.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "beever_atlas.wiki.cache.WikiCache",
            return_value=mock_cache,
        ),
        patch(
            "beever_atlas.agents.tools.memory_tools.get_recent_activity",
            new=AsyncMock(return_value=fresh_facts),
        ),
    ):
        result = await get_wiki_page(channel_id="C1", page_type="activity")

    assert result is not None, "Should return fresh content, not None"
    assert "No activity recorded" not in result["content"], (
        "Stale sentinel should be replaced by fresh facts"
    )
    assert "alice" in result["content"] or "Alice" in result["content"]


@pytest.mark.asyncio
async def test_truly_empty_activity_returns_none():
    """When stored page has the stale sentinel AND get_recent_activity
    returns nothing, get_wiki_page should return None (not echo the sentinel)."""
    from beever_atlas.agents.tools.wiki_tools import get_wiki_page

    stale_page = {
        "content": "No activity recorded in the last 7 days",
        "summary": "",
    }

    mock_cache = MagicMock()
    mock_cache.get_page = AsyncMock(return_value=stale_page)

    mock_settings = MagicMock()
    mock_settings.mongodb_uri = "mongodb://mock"

    with (
        patch(
            "beever_atlas.infra.config.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "beever_atlas.wiki.cache.WikiCache",
            return_value=mock_cache,
        ),
        patch(
            "beever_atlas.agents.tools.memory_tools.get_recent_activity",
            new=AsyncMock(return_value=[]),
        ),
    ):
        result = await get_wiki_page(channel_id="C1", page_type="activity")

    assert result is None, "Truly empty channel should return None, not the stale sentinel"


@pytest.mark.asyncio
async def test_non_stale_activity_page_returned_as_is():
    """A non-stale activity page (with real content) should be returned unchanged."""
    from beever_atlas.agents.tools.wiki_tools import get_wiki_page

    real_page = {
        "content": "## Recent Activity\n- Alice shipped the auth refactor",
        "summary": "1 recent event",
    }

    mock_cache = MagicMock()
    mock_cache.get_page = AsyncMock(return_value=real_page)

    mock_settings = MagicMock()
    mock_settings.mongodb_uri = "mongodb://mock"

    with (
        patch(
            "beever_atlas.infra.config.get_settings",
            return_value=mock_settings,
        ),
        patch(
            "beever_atlas.wiki.cache.WikiCache",
            return_value=mock_cache,
        ),
    ):
        result = await get_wiki_page(channel_id="C1", page_type="activity")

    assert result is not None
    assert result["content"] == real_page["content"]
