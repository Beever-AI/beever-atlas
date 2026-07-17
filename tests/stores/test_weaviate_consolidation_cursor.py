"""Regression tests for RES-946 — consolidation fact reads must use Weaviate
cursor pagination (``collection.iterator()``), not offset pagination.

The old ``iter_all_fact_ids`` / ``iter_unclustered_facts`` used
``fetch_objects(filters=..., offset=...)``, which is subject to Weaviate's
``QUERY_MAXIMUM_RESULTS`` (default 10000): on channels with >10k atomic facts,
``full_reconsolidate`` failed with ``query maximum results exceeded`` and the
Space wiki could never consolidate. Cursor iteration walks by UUID with no such
ceiling; ``iterator()`` takes no ``filters=`` kwarg so predicates are applied
client-side.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from beever_atlas.stores.weaviate_store import WeaviateStore


def _obj(uuid: str, channel_id: str, tier: str, cluster_id: str, *, with_vector: bool = False):
    props = {
        "channel_id": channel_id,
        "tier": tier,
        "cluster_id": cluster_id,
        "memory_text": f"text-{uuid}",
    }
    ns = SimpleNamespace(uuid=uuid, properties=props)
    if with_vector:
        ns.vector = {"default": [0.1, 0.2, 0.3]}  # named-vector shape _obj_to_fact normalises
    return ns


def _fake_collection(objs):
    fake = MagicMock(name="WeaviateCollection")
    # iterator() takes no filters= kwarg in weaviate-client v4 — it walks the
    # whole collection; predicates are applied client-side by the store.
    fake.iterator = MagicMock(side_effect=lambda **_kw: iter(objs))
    return fake


@pytest.mark.asyncio
async def test_iter_all_fact_ids_walks_cursor_past_10k_no_offset() -> None:
    """>10k atomic rows in one channel must ALL be yielded (no 10k ceiling), and
    offset pagination (``fetch_objects``) must not be used."""
    objs = [_obj(f"a{i}", "CHAN", "atomic", "__none__") for i in range(12000)]
    objs += [_obj(f"b{i}", "OTHER", "atomic", "c1") for i in range(50)]  # other channel
    objs += [_obj(f"t{i}", "CHAN", "topic", "") for i in range(20)]  # other tier

    store = WeaviateStore("http://localhost:8080")
    fake = _fake_collection(objs)
    store._collection = lambda: fake  # type: ignore[method-assign]

    out = [pair async for pair in store.iter_all_fact_ids("CHAN")]

    assert len(out) == 12000, "must yield every atomic row in the channel — no 10k ceiling"
    assert all(isinstance(u, str) for u, _ in out)
    assert {cid for _, cid in out} == {"__none__"}
    fake.query.fetch_objects.assert_not_called()  # the old offset-pagination bug
    fake.iterator.assert_called()


@pytest.mark.asyncio
async def test_iter_unclustered_facts_filters_channel_tier_cluster_with_vectors() -> None:
    """Only channel + atomic + ``cluster_id == "__none__"`` rows, with vectors,
    past the 10k mark, and no offset pagination."""
    objs = [_obj(f"u{i}", "CHAN", "atomic", "__none__", with_vector=True) for i in range(11000)]
    objs += [_obj("clustered", "CHAN", "atomic", "c9", with_vector=True)]  # already clustered
    objs += [_obj("wrongchan", "OTHER", "atomic", "__none__", with_vector=True)]
    objs += [_obj("wrongtier", "CHAN", "topic", "__none__", with_vector=True)]

    store = WeaviateStore("http://localhost:8080")
    fake = _fake_collection(objs)
    store._collection = lambda: fake  # type: ignore[method-assign]

    facts = [f async for f in store.iter_unclustered_facts("CHAN")]

    assert len(facts) == 11000, "no 10k ceiling; only the matching rows"
    assert all(f.channel_id == "CHAN" and f.tier == "atomic" for f in facts)
    assert all(f.cluster_id == "__none__" for f in facts)
    assert all(f.text_vector == [0.1, 0.2, 0.3] for f in facts), "vectors must be populated"
    fake.query.fetch_objects.assert_not_called()


@pytest.mark.asyncio
async def test_iter_unclustered_facts_empty_channel_yields_nothing() -> None:
    objs = [_obj("x", "OTHER", "atomic", "__none__", with_vector=True)]
    store = WeaviateStore("http://localhost:8080")
    store._collection = lambda: _fake_collection(objs)  # type: ignore[method-assign]
    facts = [f async for f in store.iter_unclustered_facts("CHAN")]
    assert facts == []
