"""Regression test for issue #33 — NullGraphStore.delete_channel_data
must return the canonical `*_deleted` key shape used by the GraphStore
protocol and the real implementations (Neo4j, Nebula).

The pre-fix shape `{entities, relationships, events, media}` caused
KeyError in callers that read `result["entities_deleted"]` (e.g.
api/channels.py spreads the dict into the response payload).
"""

from __future__ import annotations

import pytest

from beever_atlas.stores.null_graph import NullGraphStore


@pytest.mark.asyncio
async def test_null_graph_delete_returns_canonical_keys() -> None:
    """delete_channel_data must return exactly the protocol's `*_deleted`
    keys with int zero values — no `entities`, no `relationships`."""
    store = NullGraphStore()
    result = await store.delete_channel_data("any-channel")

    expected_keys = {"entities_deleted", "events_deleted", "media_deleted"}
    assert set(result.keys()) == expected_keys, (
        f"NullGraphStore returned {set(result.keys())}, "
        f"expected {expected_keys} to match Neo4j/Nebula protocol"
    )
    assert all(isinstance(v, int) and v == 0 for v in result.values())


@pytest.mark.asyncio
async def test_null_graph_delete_no_keyerror_on_canonical_lookup() -> None:
    """The originally-reported failure mode: callers do
    `result["entities_deleted"]` and KeyError when the key is just
    `entities`. After the fix, that lookup succeeds."""
    store = NullGraphStore()
    result = await store.delete_channel_data("any-channel")

    # These must NOT raise.
    assert result["entities_deleted"] == 0
    assert result["events_deleted"] == 0
    assert result["media_deleted"] == 0
