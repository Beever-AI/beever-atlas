"""Regression tests: true_hybrid_search and semantic_search must NOT send
is_none(True) filters to Weaviate, and must post-filter superseded facts
Python-side instead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fact(invalid_at=None):
    """Return a minimal AtomicFact-like mock."""
    fact = MagicMock()
    fact.invalid_at = invalid_at
    fact.id = "fact-1"
    return fact


def _make_obj(invalid_at=None, score=0.8, distance=0.2):
    obj = MagicMock()
    obj.properties = {}
    obj.metadata.score = score
    obj.metadata.distance = distance
    obj.uuid = "fact-1"
    return obj


def _store():
    from beever_atlas.stores.weaviate_store import WeaviateStore

    return WeaviateStore(url="http://localhost:8080")


# ---------------------------------------------------------------------------
# true_hybrid_search: no is_none in filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_true_hybrid_search_no_is_none_filter():
    """true_hybrid_search must NOT pass any is_none filter to Weaviate."""
    store = _store()

    fake_result = MagicMock()
    fake_result.objects = []

    fake_collection = MagicMock()
    fake_collection.query.hybrid.return_value = fake_result

    with patch.object(store, "_collection", return_value=fake_collection):
        await store.true_hybrid_search(
            query_text="test",
            query_vector=[0.0] * 10,
            channel_id="C1",
            include_superseded=False,
        )

    call_kwargs = fake_collection.query.hybrid.call_args.kwargs
    filters = call_kwargs.get("filters")
    # Serialise to string and assert no is_none / nullstate in there
    filters_repr = repr(filters)
    assert "is_none" not in filters_repr.lower(), f"is_none found in filter: {filters_repr}"


@pytest.mark.asyncio
async def test_true_hybrid_search_post_filters_superseded():
    """true_hybrid_search must exclude facts with invalid_at != None by default
    and include them when include_superseded=True."""
    store = _store()

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fact_active = _make_fact(invalid_at=None)
    fact_superseded = _make_fact(invalid_at=now)

    obj_active = _make_obj()
    obj_superseded = _make_obj()

    fake_result = MagicMock()
    fake_result.objects = [obj_active, obj_superseded]

    fake_collection = MagicMock()
    fake_collection.query.hybrid.return_value = fake_result

    side_effects = [fact_active, fact_superseded]

    # Default: exclude superseded
    with (
        patch.object(store, "_collection", return_value=fake_collection),
        patch.object(store, "_obj_to_fact", side_effect=side_effects),
    ):
        results = await store.true_hybrid_search(
            query_text="test",
            query_vector=[0.0] * 10,
            channel_id="C1",
            include_superseded=False,
        )

    assert len(results) == 1
    assert results[0]["fact"] is fact_active

    # include_superseded=True: both returned
    fake_result2 = MagicMock()
    fake_result2.objects = [obj_active, obj_superseded]
    fake_collection2 = MagicMock()
    fake_collection2.query.hybrid.return_value = fake_result2

    with (
        patch.object(store, "_collection", return_value=fake_collection2),
        patch.object(store, "_obj_to_fact", side_effect=[fact_active, fact_superseded]),
    ):
        results_all = await store.true_hybrid_search(
            query_text="test",
            query_vector=[0.0] * 10,
            channel_id="C1",
            include_superseded=True,
        )

    assert len(results_all) == 2


# ---------------------------------------------------------------------------
# semantic_search: no is_none in filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_no_is_none_filter():
    """semantic_search must NOT pass any is_none filter to Weaviate."""
    store = _store()

    fake_result = MagicMock()
    fake_result.objects = []

    fake_collection = MagicMock()
    fake_collection.query.near_vector.return_value = fake_result

    with patch.object(store, "_collection", return_value=fake_collection):
        await store.semantic_search(
            query_vector=[0.0] * 10,
            channel_id="C1",
            include_superseded=False,
        )

    call_kwargs = fake_collection.query.near_vector.call_args.kwargs
    filters = call_kwargs.get("filters")
    filters_repr = repr(filters)
    assert "is_none" not in filters_repr.lower(), f"is_none found in filter: {filters_repr}"


@pytest.mark.asyncio
async def test_semantic_search_post_filters_superseded():
    """semantic_search must exclude superseded facts by default, include with
    include_superseded=True."""
    store = _store()

    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    fact_active = _make_fact(invalid_at=None)
    fact_superseded = _make_fact(invalid_at=now)

    # distance of 0.2 -> similarity 0.8, above default threshold
    obj_active = _make_obj(distance=0.2)
    obj_superseded = _make_obj(distance=0.2)

    fake_result = MagicMock()
    fake_result.objects = [obj_active, obj_superseded]

    fake_collection = MagicMock()
    fake_collection.query.near_vector.return_value = fake_result

    # Default: exclude superseded
    with (
        patch.object(store, "_collection", return_value=fake_collection),
        patch.object(store, "_obj_to_fact", side_effect=[fact_active, fact_superseded]),
    ):
        results = await store.semantic_search(
            query_vector=[0.0] * 10,
            channel_id="C1",
            threshold=0.5,
            include_superseded=False,
        )

    assert len(results) == 1
    assert results[0]["fact"] is fact_active

    # include_superseded=True: both returned
    fake_result2 = MagicMock()
    fake_result2.objects = [obj_active, obj_superseded]
    fake_collection2 = MagicMock()
    fake_collection2.query.near_vector.return_value = fake_result2

    with (
        patch.object(store, "_collection", return_value=fake_collection2),
        patch.object(store, "_obj_to_fact", side_effect=[fact_active, fact_superseded]),
    ):
        results_all = await store.semantic_search(
            query_vector=[0.0] * 10,
            channel_id="C1",
            threshold=0.5,
            include_superseded=True,
        )

    assert len(results_all) == 2
