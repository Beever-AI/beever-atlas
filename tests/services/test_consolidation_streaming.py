"""Regression tests for streaming pagination in consolidation.

Protects against the gRPC 10MB response cap that previously broke
``full_reconsolidate`` on large channels.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from beever_atlas.services.consolidation import ConsolidationService


async def _aiter(items):
    for item in items:
        yield item


def _make_service(store) -> ConsolidationService:
    settings = SimpleNamespace(
        cluster_similarity_threshold=0.85,
        cluster_merge_threshold=0.9,
        cluster_max_size=100,
    )
    return ConsolidationService(weaviate=store, settings=settings)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_full_reconsolidate_streams_all_fact_ids_and_batches_updates():
    """Reset path must iterate every fact via iter_all_fact_ids and batch writes."""
    # 1250 facts across 3 full pages (500, 500) + a tail (250) — exercises batch flushing.
    fact_records = [(f"fact-{i}", "cluster-x") for i in range(1250)]

    store = AsyncMock()
    store.list_clusters = AsyncMock(return_value=[])
    store.delete_cluster = AsyncMock()
    store.iter_all_fact_ids = lambda channel_id: _aiter(fact_records)
    store.batch_update_fact_clusters = AsyncMock()
    # Short-circuit the rest of the pipeline — we only care about the reset path.
    store.get_unclustered_facts = AsyncMock(return_value=[])

    service = _make_service(store)
    result = await service.full_reconsolidate("C_TEST", channel_name="test")

    assert result.errors == []

    # Every fact_id with a non-sentinel cluster_id must have been reset.
    flat = [
        pair for call in store.batch_update_fact_clusters.await_args_list for pair in call.args[0]
    ]
    assert len(flat) == 1250
    assert all(cid == "__none__" for _, cid in flat)

    # Batches should be ≤500 each (the BATCH constant). No unbounded single call.
    for call in store.batch_update_fact_clusters.await_args_list:
        assert len(call.args[0]) <= 500


@pytest.mark.asyncio
async def test_full_reconsolidate_skips_already_unclustered_facts():
    """Facts already at '__none__' must not generate redundant updates."""
    records = [
        ("a", "cluster-1"),
        ("b", "__none__"),
        ("c", "cluster-2"),
        ("d", "__none__"),
    ]
    store = AsyncMock()
    store.list_clusters = AsyncMock(return_value=[])
    store.delete_cluster = AsyncMock()
    store.iter_all_fact_ids = lambda channel_id: _aiter(records)
    store.batch_update_fact_clusters = AsyncMock()
    store.get_unclustered_facts = AsyncMock(return_value=[])

    await _make_service(store).full_reconsolidate("C_TEST")

    updated_ids = [
        fid for call in store.batch_update_fact_clusters.await_args_list for fid, _ in call.args[0]
    ]
    assert set(updated_ids) == {"a", "c"}
