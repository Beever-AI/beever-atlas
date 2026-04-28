"""Regression tests for issue #32 — `delete_by_channel` must use Weaviate's
batch `data.delete_many` (no 10k limit), not fetch+loop with `limit=10000`
which silently dropped objects beyond the first 10000 in large channels.

`delete_all` is dev-only and uses fetch+delete in a loop until empty;
the loop is verified to drain all objects across multiple pages.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from beever_atlas.stores.weaviate_store import WeaviateStore


@pytest.mark.asyncio
async def test_delete_by_channel_uses_batch_delete_many() -> None:
    """`delete_by_channel` must call `collection.data.delete_many(where=...)`
    once with the channel filter, instead of fetching + looping. Returns the
    server-reported count via `result.successful`."""
    store = WeaviateStore("http://localhost:8080")

    # Stub the v4 collection so we can inspect the call to data.delete_many.
    fake_collection = MagicMock(name="WeaviateCollection")
    fake_collection.data.delete_many = MagicMock(
        return_value=SimpleNamespace(successful=15234, failed=0, matches=15234)
    )
    # delete_by_channel runs in a thread pool — the underlying _collection() call
    # is sync, so we override it.
    store._collection = lambda: fake_collection  # type: ignore[method-assign]

    deleted = await store.delete_by_channel("CHAN_BIG")

    assert deleted == 15234, "must return result.successful from delete_many"
    fake_collection.data.delete_many.assert_called_once()
    # Argument is `where=<Filter>`. We check the keyword name was used; the
    # filter object itself is opaque from the test's perspective.
    _, kwargs = fake_collection.data.delete_many.call_args
    assert "where" in kwargs, "delete_many must be called with a `where=` filter"
    assert kwargs["where"] is not None
    # Importantly: fetch_objects must NOT have been called — the bug was the
    # 10000-limit fetch+loop.
    fake_collection.query.fetch_objects.assert_not_called()


@pytest.mark.asyncio
async def test_delete_all_drains_in_pages_beyond_10000() -> None:
    """`delete_all` (dev-only) loops fetch+delete until the collection is empty.
    Simulates a collection of 25,000 objects in 3 pages of 1000+1000+remainder
    and verifies all are deleted (issue #32 — the old `limit=10000` would have
    stopped at 10000)."""
    store = WeaviateStore("http://localhost:8080")

    # Build 3 pages: 1000, 1000, 500 objects, then empty to terminate the loop.
    page1 = [SimpleNamespace(uuid=f"u{i}") for i in range(1000)]
    page2 = [SimpleNamespace(uuid=f"v{i}") for i in range(1000)]
    page3 = [SimpleNamespace(uuid=f"w{i}") for i in range(500)]

    fetch_returns = iter(
        [
            SimpleNamespace(objects=page1),
            SimpleNamespace(objects=page2),
            SimpleNamespace(objects=page3),
            SimpleNamespace(objects=[]),  # terminator
        ]
    )

    fake_collection = MagicMock(name="WeaviateCollection")
    fake_collection.query.fetch_objects = MagicMock(side_effect=lambda **_kw: next(fetch_returns))
    fake_collection.data.delete_by_id = MagicMock()
    store._collection = lambda: fake_collection  # type: ignore[method-assign]

    deleted = await store.delete_all()

    assert deleted == 2500, f"expected 2500 across 3 pages, got {deleted}"
    # 4 fetch calls: 3 with data + 1 empty terminator
    assert fake_collection.query.fetch_objects.call_count == 4
    # delete_by_id called once per object
    assert fake_collection.data.delete_by_id.call_count == 2500


@pytest.mark.asyncio
async def test_delete_by_channel_zero_matches_returns_zero() -> None:
    """When no objects match, delete_many returns successful=0."""
    store = WeaviateStore("http://localhost:8080")

    fake_collection = MagicMock(name="WeaviateCollection")
    fake_collection.data.delete_many = MagicMock(
        return_value=SimpleNamespace(successful=0, failed=0, matches=0)
    )
    store._collection = lambda: fake_collection  # type: ignore[method-assign]

    deleted = await store.delete_by_channel("EMPTY_CHAN")

    assert deleted == 0
