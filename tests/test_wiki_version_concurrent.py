"""Concurrent archive() must produce distinct, monotonically-increasing version numbers.

Emulates MongoDB's atomic ``find_one_and_update`` ``$inc`` semantics with an
async lock so the shared counter is incremented serially even under
``asyncio.gather`` fan-out.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.wiki.version_store import WikiVersionStore


@pytest.mark.asyncio
async def test_concurrent_archive_assigns_distinct_monotonic_versions():
    # State shared between the mocked collection and counters collection.
    state = {"seq": 0, "inserts": [], "lock": asyncio.Lock()}

    # --- primary wiki_versions collection ---
    collection = MagicMock()

    async def col_find_one(*_args, **_kwargs):
        # Used by the backfill branch of _next_version_number to discover the
        # highest existing version_number. Always None here — fresh channel.
        return None

    async def col_insert_one(doc):
        state["inserts"].append(doc)

    collection.find_one = AsyncMock(side_effect=col_find_one)
    collection.insert_one = AsyncMock(side_effect=col_insert_one)

    # --- wiki_version_counters collection (atomic $inc target) ---
    counters = MagicMock()

    async def counters_find_one(*_args, **_kwargs):
        # First call per channel returns None so the backfill upsert runs.
        # After that the counter doc exists (seq tracked in state).
        if state["seq"] == 0 and not state["inserts"]:
            return None
        return {"_id": "C1", "seq": state["seq"]}

    async def counters_update_one(*_args, **_kwargs):
        # Backfill $max upsert — no state change required for this test
        # (seed is 0 because col_find_one returned None).
        return MagicMock(acknowledged=True)

    async def counters_find_one_and_update(*_args, **_kwargs):
        # Emulate atomic $inc: hold a lock so concurrent callers serialize
        # exactly as Mongo would.
        async with state["lock"]:
            state["seq"] += 1
            return {"_id": "C1", "seq": state["seq"]}

    counters.find_one = AsyncMock(side_effect=counters_find_one)
    counters.update_one = AsyncMock(side_effect=counters_update_one)
    counters.find_one_and_update = AsyncMock(side_effect=counters_find_one_and_update)

    store = WikiVersionStore("mongodb://test")
    store._collection = collection  # bypass _ensure_db
    store._counters = counters

    wiki = {"pages": {}, "metadata": {}}
    results = await asyncio.gather(*[store.archive("C1", wiki) for _ in range(20)])

    assert len(results) == 20
    assert len(set(results)) == 20, "version numbers must be distinct"
    assert results == sorted(results), "version numbers must be monotonic"
    assert results[0] == 1
    assert results[-1] == 20
