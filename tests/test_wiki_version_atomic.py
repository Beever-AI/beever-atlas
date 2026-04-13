"""Concurrency tests for WikiVersionStore.archive atomic numbering."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.wiki.version_store import WikiVersionStore


def _make_counters_mock(initial_seq: int = 0) -> MagicMock:
    """Return a mocked counters collection that atomically increments.

    Uses a single ``asyncio.Lock`` so the counter state cannot be read and
    written racily across concurrent calls — the same guarantee MongoDB's
    ``find_one_and_update`` provides in production.
    """
    state = {"seq": initial_seq, "exists": initial_seq > 0}
    lock = asyncio.Lock()

    async def _find_one(query):
        async with lock:
            return {"_id": query["_id"], "seq": state["seq"]} if state["exists"] else None

    async def _update_one(query, update, upsert=False):
        async with lock:
            if "$max" in update:
                state["seq"] = max(state["seq"], update["$max"]["seq"])
            state["exists"] = True
            return MagicMock(matched_count=1)

    async def _find_one_and_update(query, update, upsert=False, return_document=None):
        async with lock:
            if "$inc" in update:
                state["seq"] += update["$inc"]["seq"]
            state["exists"] = True
            return {"_id": query["_id"], "seq": state["seq"]}

    mock = MagicMock()
    mock.find_one = AsyncMock(side_effect=_find_one)
    mock.update_one = AsyncMock(side_effect=_update_one)
    mock.find_one_and_update = AsyncMock(side_effect=_find_one_and_update)
    return mock


def _make_store_with_mocks(counters_mock) -> WikiVersionStore:
    store = WikiVersionStore(mongodb_uri="mongodb://unused", db_name="test")
    store._collection = MagicMock()
    store._collection.insert_one = AsyncMock()
    store._collection.find_one = AsyncMock(return_value=None)
    store._counters = counters_mock
    return store


@pytest.mark.asyncio
async def test_concurrent_archive_assigns_distinct_version_numbers():
    counters = _make_counters_mock()
    store = _make_store_with_mocks(counters)

    async def _archive() -> int:
        return await store.archive("C123", {"channel_name": "test", "pages": {}})

    results = await asyncio.gather(*[_archive() for _ in range(20)])

    assert sorted(results) == list(range(1, 21)), (
        f"Concurrent archive produced duplicate or missing version numbers: {results}"
    )


@pytest.mark.asyncio
async def test_next_version_number_uses_find_one_and_update():
    counters = _make_counters_mock()
    store = _make_store_with_mocks(counters)

    n = await store._next_version_number("C123")
    assert n == 1
    counters.find_one_and_update.assert_called()
    call_kwargs = counters.find_one_and_update.call_args.kwargs
    assert call_kwargs.get("upsert") is True
    call_args = counters.find_one_and_update.call_args.args
    update_doc = call_args[1] if len(call_args) > 1 else call_kwargs["update"]
    assert "$inc" in update_doc
    assert update_doc["$inc"]["seq"] == 1


@pytest.mark.asyncio
async def test_backfill_seeds_counter_from_existing_versions():
    """When no counter exists but old version rows do, the counter seeds
    from ``max(version_number)`` so new versions don't collide with old."""
    counters = _make_counters_mock(initial_seq=0)  # counter absent
    store = _make_store_with_mocks(counters)
    # Simulate 7 pre-existing versions in the wiki_versions collection.
    store._collection.find_one = AsyncMock(return_value={"version_number": 7})

    n = await store._next_version_number("C123")
    assert n == 8
    # Second call should go straight through the increment path.
    n2 = await store._next_version_number("C123")
    assert n2 == 9
