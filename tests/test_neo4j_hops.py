"""Regression tests for Neo4jStore.get_neighbors.

1. ``hops`` is clamped to [1, 4] — prevents attacker-controlled `hops` from
   building unbounded Cypher path patterns like `-[r*1..1000]-`.
2. The neighborhood is read by iterating RAW driver records, NOT
   ``result.data()``. ``result.data()`` serialises a Relationship value into a
   3-tuple ``(start_node, "TYPE", end_node)`` which drops the relationship's own
   properties and makes ``_rel_from_record`` raise
   ``ValueError: dictionary update sequence element #0 has length 12`` — so every
   deep-mode traversal silently returned zero edges.
"""

from __future__ import annotations

import pytest


class _FakeRel(dict):
    """Stand-in for a Neo4j Relationship: a property mapping (so ``dict(rel)``
    works) that also carries ``element_id`` / ``type`` attributes."""

    def __init__(self, props, element_id, rtype):
        super().__init__(props)
        self.element_id = element_id
        self.type = rtype


class _FakeResult:
    def __init__(self, rows=None):
        self._rows = rows or []
        self.data_called = False

    async def data(self):
        # If get_neighbors ever calls this again, the regression is back.
        self.data_called = True
        return []

    def __aiter__(self):
        async def _gen():
            for row in self._rows:
                yield row

        return _gen()


class _FakeSession:
    rows: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def run(self, query, **params):
        _FakeSession.last_query = query
        _FakeSession.last_params = params
        _FakeSession.last_result = _FakeResult(_FakeSession.rows)
        return _FakeSession.last_result


class _FakeDriver:
    def session(self):
        return _FakeSession()


@pytest.mark.asyncio
async def test_get_neighbors_clamps_hops_to_max_4():
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    store._driver = _FakeDriver()
    _FakeSession.rows = []

    await store.get_neighbors("some-eid", hops=99, limit=10)

    assert "[r*1..4]" in _FakeSession.last_query
    assert "[r*1..99]" not in _FakeSession.last_query


@pytest.mark.asyncio
async def test_get_neighbors_clamps_hops_floor_to_1():
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    store._driver = _FakeDriver()
    _FakeSession.rows = []

    await store.get_neighbors("some-eid", hops=0, limit=10)

    assert "[r*1..1]" in _FakeSession.last_query


@pytest.mark.asyncio
async def test_get_neighbors_builds_edges_from_raw_relationship_objects():
    """The relationship's own properties must survive into the edge, and
    ``result.data()`` must NOT be used (it would discard them and crash)."""
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    store._driver = _FakeDriver()
    _FakeSession.rows = [
        {
            "src_node": {"name": "Alan Yang", "properties": "{}"},
            "tgt_node": {"name": "DOCX File", "properties": "{}"},
            "rel": _FakeRel(
                {"confidence": 0.9, "context": "will check them"},
                element_id="5:abc:0",
                rtype="ACTS_ON",
            ),
        }
    ]

    sub = await store.get_neighbors("some-eid", hops=1, limit=10)

    assert not _FakeSession.last_result.data_called, "must not use result.data()"
    assert len(sub.edges) == 1
    edge = sub.edges[0]
    assert edge.type == "ACTS_ON"
    assert edge.source == "Alan Yang"
    assert edge.target == "DOCX File"
    assert edge.confidence == pytest.approx(0.9)
    assert edge.context == "will check them"
    assert {n.name for n in sub.nodes} == {"Alan Yang", "DOCX File"}
