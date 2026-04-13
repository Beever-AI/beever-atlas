"""Regression: hops in Neo4jStore.get_neighbors is clamped to [1, 4].

Prevents attacker-controlled `hops` from building unbounded Cypher path
patterns like `-[r*1..1000]-` that can exhaust the server.
"""

from __future__ import annotations

import pytest


class _FakeResult:
    async def data(self):
        return []


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, **params):
        _FakeSession.last_query = query
        _FakeSession.last_params = params
        return _FakeResult()


class _FakeDriver:
    def session(self):
        return _FakeSession()


@pytest.mark.asyncio
async def test_get_neighbors_clamps_hops_to_max_4():
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    store._driver = _FakeDriver()

    await store.get_neighbors("some-eid", hops=99, limit=10)

    assert "[r*1..4]" in _FakeSession.last_query
    assert "[r*1..99]" not in _FakeSession.last_query


@pytest.mark.asyncio
async def test_get_neighbors_clamps_hops_floor_to_1():
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    store._driver = _FakeDriver()

    await store.get_neighbors("some-eid", hops=0, limit=10)

    assert "[r*1..1]" in _FakeSession.last_query
