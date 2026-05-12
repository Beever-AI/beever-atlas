"""Tests for WikiDataGatherer consolidation wait-loop behaviour.

Covers the race between wiki refresh and an in-progress consolidation run:

1. test_no_clusters_raises_immediately   — 0 clusters → WikiNotReadyError fast
2. test_consolidation_completes_within_window — summaries land after 2 polls
3. test_timeout_raises_wiki_not_ready   — clusters stay unsummarized for full window
4. test_api_returns_503_on_wiki_not_ready — _run_generation catches WikiNotReadyError
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.errors import WikiNotReadyError
from beever_atlas.wiki import data_gatherer as dg_mod
from beever_atlas.wiki.data_gatherer import WikiDataGatherer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cluster(summary: str = "") -> SimpleNamespace:
    """Return a minimal cluster-like object."""
    return SimpleNamespace(
        id="cluster-1",
        summary=summary,
        faq_candidates=[],
        member_count=1,
    )


def _make_channel_summary() -> SimpleNamespace:
    """Return a minimal ChannelSummary-like object."""
    return SimpleNamespace(
        channel_name="#test",
        fact_count=5,
        glossary_terms=[],
        media_count=0,
        cluster_count=1,
    )


def _make_weaviate(
    *,
    channel_summary=None,
    clusters=None,
) -> MagicMock:
    weaviate = MagicMock()
    weaviate.get_channel_summary = AsyncMock(return_value=channel_summary)
    weaviate.list_clusters = AsyncMock(return_value=clusters or [])
    weaviate.fetch_recent_facts = AsyncMock(return_value=[])
    weaviate.fetch_media_facts = AsyncMock(return_value=[])
    weaviate.count_facts = AsyncMock(return_value=0)
    weaviate.fetch_all_cluster_members = AsyncMock(return_value=[])
    return weaviate


def _make_graph() -> MagicMock:
    graph = MagicMock()
    graph.list_person_entities_with_edges = AsyncMock(return_value=[])
    graph.get_decisions_with_chains = AsyncMock(return_value=[])
    graph.list_technology_entities = AsyncMock(return_value=[])
    graph.list_project_entities = AsyncMock(return_value=[])
    graph.count_entities = AsyncMock(return_value=0)
    return graph


# ---------------------------------------------------------------------------
# Test 1 — zero clusters → raises immediately (no 15-s wait)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_clusters_raises_immediately():
    """When get_channel_summary returns None AND list_clusters returns []
    the first poll should detect no clusters and raise WikiNotReadyError
    without waiting the full 15 s.
    """
    weaviate = _make_weaviate(channel_summary=None, clusters=[])
    graph = _make_graph()
    gatherer = WikiDataGatherer(weaviate, graph)

    # Speed up the wait by shrinking the poll interval to 0
    with patch.object(dg_mod, "_CONSOLIDATION_POLL_INTERVAL", 0):
        with pytest.raises(WikiNotReadyError, match="not been consolidated"):
            await gatherer.gather("chan-123")

    # Should have polled list_clusters exactly once after the initial gather
    # (the initial asyncio.gather call + the first retry poll = 2 calls total).
    assert weaviate.list_clusters.call_count >= 1


# ---------------------------------------------------------------------------
# Test 2 — summaries land after 2 polls → gather succeeds
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidation_completes_within_window():
    """channel_summary is None on the first call but returns a real object
    on the third call (simulating consolidation catching up mid-wait).
    """
    channel_summary = _make_channel_summary()
    cluster = _make_cluster(summary="Some summary")

    weaviate = _make_weaviate(channel_summary=None, clusters=[cluster])
    # get_channel_summary: first two calls return None, third returns real object
    weaviate.get_channel_summary = AsyncMock(
        side_effect=[
            None,  # initial asyncio.gather call
            None,  # poll 1
            channel_summary,  # poll 2 — consolidation done
        ]
    )
    # list_clusters always returns the cluster (it exists, just not summarized yet)
    weaviate.list_clusters = AsyncMock(return_value=[cluster])

    graph = _make_graph()
    gatherer = WikiDataGatherer(weaviate, graph)

    with patch.object(dg_mod, "_CONSOLIDATION_POLL_INTERVAL", 0):
        result = await gatherer.gather("chan-123")

    assert result["channel_summary"] is channel_summary
    # Should have called get_channel_summary at least 3 times
    assert weaviate.get_channel_summary.call_count >= 3


# ---------------------------------------------------------------------------
# Test 3 — clusters stay unsummarized for entire window → WikiNotReadyError
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_raises_wiki_not_ready():
    """channel_summary stays None for all 30 polls. After exhausting the
    wait window the gatherer raises WikiNotReadyError("still in progress").
    """
    cluster = _make_cluster(summary="")
    weaviate = _make_weaviate(channel_summary=None, clusters=[cluster])
    graph = _make_graph()
    gatherer = WikiDataGatherer(weaviate, graph)

    # Speed up the entire wait by zeroing the interval AND shrinking the
    # step count to 3 so the test doesn't spin 30 real iterations.
    with (
        patch.object(dg_mod, "_CONSOLIDATION_POLL_INTERVAL", 0),
        patch.object(dg_mod, "_CONSOLIDATION_POLL_STEPS", 3),
    ):
        with pytest.raises(WikiNotReadyError, match="still in progress"):
            await gatherer.gather("chan-123")


# ---------------------------------------------------------------------------
# Test 4 — _run_generation surfaces WikiNotReadyError as "not_ready" status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_returns_503_on_wiki_not_ready():
    """_run_generation must catch WikiNotReadyError and call
    cache.set_generation_status with status='not_ready' rather than 'failed'.
    """
    from beever_atlas.api.wiki import _run_generation

    cache = MagicMock()
    cache.set_generation_status = AsyncMock()
    cache.delete_wiki = AsyncMock()

    builder = MagicMock()
    builder.refresh_wiki = AsyncMock(
        side_effect=WikiNotReadyError("Consolidation is still in progress. Retry in a few seconds.")
    )

    await _run_generation(
        builder=builder,
        channel_id="chan-456",
        cache=cache,
        target_lang="en",
    )

    # set_generation_status must have been called with status="not_ready"
    calls = cache.set_generation_status.call_args_list
    statuses = [c.kwargs.get("status") or (c.args[1] if len(c.args) > 1 else None) for c in calls]
    assert "not_ready" in statuses, f"Expected 'not_ready' in status calls, got: {statuses}"
