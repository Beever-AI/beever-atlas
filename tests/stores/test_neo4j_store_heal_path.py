"""Tests for the Unresolved-stub heal-path in :mod:`neo4j_store`.

Covers the four user-facing guarantees:

  1. ``_upsert_relationship_with_stub_flag`` typed Unresolved (not Topic)
     for both endpoints when both are unknown.
  2. A subsequent typed ``upsert_entity`` promotes the Unresolved stub
     in place — Cypher is a MATCH+SET, no second MERGE row.
  3. ``prune_stub_orphans`` deletes Unresolved+awaiting_type+no-edges
     entities older than the TTL; preserves ones with edges or younger.
  4. ``upsert_wiki_reference_entity_edge`` is idempotent.

All tests use the in-process mock driver pattern established by
``tests/integration/test_relationship_stub_merge.py`` — no live Neo4j
required.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.models.domain import GraphEntity, GraphRelationship


# ── shared mock-driver helpers (mirrors test_relationship_stub_merge) ──────


def _make_neo4j_store_with_capture():
    """Build a Neo4jStore with a mocked async driver and capture every
    ``session.run`` call (query, kwargs).

    Returns ``(store, calls, set_single_result)``.
    """
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[dict] = []
    next_single = {"value": None}
    next_data = {"value": []}

    mock_result = MagicMock()

    async def _single():
        return next_single["value"]

    async def _data():
        return next_data["value"]

    mock_result.single = _single
    mock_result.data = _data

    mock_session = AsyncMock()

    async def _run(query, **kwargs):
        calls.append({"query": query, "kwargs": kwargs})
        return mock_result

    mock_session.run = _run
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    mock_driver = MagicMock()
    mock_driver.session = MagicMock(return_value=mock_session)
    store._driver = mock_driver

    def set_single_result(value):
        next_single["value"] = value

    return store, calls, set_single_result


def _force_settings(monkeypatch, *, stub_endpoints: bool) -> None:
    from beever_atlas.infra import config as _config_mod

    real_settings = _config_mod.get_settings()
    object.__setattr__(real_settings, "neo4j_relationship_stub_endpoints", stub_endpoints)


# ── 1. Unknown endpoints create Unresolved stubs ───────────────────────────


@pytest.mark.asyncio
async def test_relationship_two_unknown_endpoints_create_unresolved_stubs(monkeypatch):
    """Both endpoints unknown → 2 stubs typed ``Unresolved`` (not
    ``Topic``) with ``awaiting_type`` in stub_props.
    """
    _force_settings(monkeypatch, stub_endpoints=True)
    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"eid": "rel-heal-1", "stubs_created": 2})

    rel = GraphRelationship(
        source="MysteryA",
        target="MysteryB",
        type="MENTIONS",
        confidence=0.9,
    )
    eid, stub_count = await store._upsert_relationship_with_stub_flag(rel)

    assert eid == "rel-heal-1"
    assert stub_count == 2
    assert len(calls) == 1
    query = calls[0]["query"]
    kwargs = calls[0]["kwargs"]
    # Both endpoints MERGEd as Unresolved (variables renamed to _raw
    # because they may be merged into a typed sibling immediately via
    # apoc.refactor.mergeNodes — the symmetric heal pattern).
    assert "MERGE (a_raw:Entity {name: $source, type: 'Unresolved', scope: 'global'})" in query
    assert "MERGE (b_raw:Entity {name: $target, type: 'Unresolved', scope: 'global'})" in query
    # The new symmetric-heal step references 'Topic' in the WHERE
    # NOT IN clause that finds typed siblings to absorb the stub into.
    # That's expected — what we do NOT want is creating a Topic stub.
    assert "MERGE (a_raw:Entity {name: $source, type: 'Topic'" not in query
    assert "MERGE (b_raw:Entity {name: $target, type: 'Topic'" not in query
    # awaiting_type set both inside stub_props JSON and as a top-level
    # node property (so prune/heal Cypher can filter on it).
    assert kwargs["stub_props"] == (
        '{"stub": true, "reason": "rel_endpoint", "awaiting_type": true}'
    )
    assert "a_raw.awaiting_type = true" in query
    assert "b_raw.awaiting_type = true" in query
    # Symmetric-heal step: OPTIONAL MATCH for typed siblings, then
    # apoc.refactor.mergeNodes absorbs the stub into the typed row.
    assert "apoc.refactor.mergeNodes" in query
    assert "WHERE NOT a_typed.type IN ['Unresolved', 'Topic']" in query
    assert "WHERE NOT b_typed.type IN ['Unresolved', 'Topic']" in query


# ── 2. Typed upsert_entity promotes Unresolved stub in place ───────────────


@pytest.mark.asyncio
async def test_upsert_entity_heal_path_promotes_unresolved(monkeypatch):
    """A typed ``upsert_entity`` (e.g. Person) for a name that already
    has an Unresolved stub now emits ONE Cypher block: MERGE the typed
    row, then ``apoc.refactor.mergeNodes`` absorbs the stub sibling
    (and any legacy Topic sibling). The mock can't distinguish "merged
    stub" from "fresh typed write" — it just verifies the new shape.
    """
    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"eid": "ent-healed-1"})

    entity = GraphEntity(
        name="Alice",
        type="Person",
        scope="global",
    )
    eid = await store.upsert_entity(entity)

    assert eid == "ent-healed-1"
    # Exactly one query — the symmetric heal runs MERGE + sibling
    # absorption in a single Cypher block.
    assert len(calls) == 1
    query = calls[0]["query"]
    assert "MERGE (typed:Entity {name: $name, type: $type, scope: 'global'})" in query
    # Sibling absorption ladder: OPTIONAL MATCH for Unresolved/Topic
    # siblings, then CALL apoc.refactor.mergeNodes.
    assert "OPTIONAL MATCH (sib:Entity {name: $name, scope: 'global'})" in query
    assert "sib.type IN ['Unresolved', 'Topic']" in query
    assert "elementId(sib) <> elementId(typed)" in query
    assert "apoc.refactor.mergeNodes" in query
    assert "mergeRels: true" in query
    # Old SET-only heal pattern is gone.
    assert "SET e.type = $type" not in query
    assert "REMOVE e.awaiting_type" not in query


@pytest.mark.asyncio
async def test_upsert_entity_heal_path_no_stub_creates_typed_row(monkeypatch):
    """When no Unresolved/Topic stub exists for this name, the same
    single Cypher block still runs — mergeNodes is a no-op on a
    single-element list (the CASE guard avoids the call entirely)."""
    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"eid": "ent-fresh-1"})

    entity = GraphEntity(
        name="Bob",
        type="Person",
        scope="global",
    )
    eid = await store.upsert_entity(entity)

    assert eid == "ent-fresh-1"
    # Single query — no fall-through to a separate typed MERGE block.
    assert len(calls) == 1
    query = calls[0]["query"]
    assert "MERGE (typed:Entity {name: $name, type: $type, scope: 'global'})" in query
    assert "apoc.refactor.mergeNodes" in query
    # The CASE guard ensures mergeNodes isn't called noisily on an
    # empty sibling list — the [typed] + sibs ladder appears.
    assert "CASE WHEN size(sibs) > 0 THEN [typed] + sibs ELSE [typed] END" in query


@pytest.mark.asyncio
async def test_upsert_entity_with_unresolved_type_skips_heal_pre_check(monkeypatch):
    """When the new entity itself is typed ``Unresolved`` (defensive —
    should not happen from the LLM), the heal pre-check is skipped so
    we don't infinite-loop healing ourselves.
    """
    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"eid": "ent-unresolved-1"})

    entity = GraphEntity(
        name="MaybeName",
        type="Unresolved",
        scope="global",
    )
    eid = await store.upsert_entity(entity)
    assert eid == "ent-unresolved-1"
    # Only one query — the typed MERGE, not the heal MATCH.
    assert len(calls) == 1
    query = calls[0]["query"]
    # No heal MATCH — straight to the MERGE.
    assert "MATCH (e:Entity {name: $name, scope: 'global', type: 'Unresolved'})" not in query
    assert "MERGE (e:Entity {name: $name, type: $type, scope: 'global'})" in query


# ── 3. prune_stub_orphans ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_stub_orphans_returns_count(monkeypatch):
    """``prune_stub_orphans`` returns the count from the Cypher query
    and issues the expected predicate (type IN Unresolved/Topic,
    no edges, older than cutoff). The ``awaiting_type`` filter was
    dropped so legacy Topic stubs (which never carried the flag) are
    also reaped once edge-less.
    """
    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"n": 3})

    purged = await store.prune_stub_orphans(ttl_hours=24)

    assert purged == 3
    assert len(calls) == 1
    query = calls[0]["query"]
    assert "e.type IN ['Unresolved', 'Topic']" in query
    assert "NOT EXISTS { MATCH (e)--() }" in query
    assert "e.created_at < $cutoff" in query
    assert "DETACH DELETE e" in query
    # The cutoff kwarg should be an ISO timestamp string.
    assert "cutoff" in calls[0]["kwargs"]
    assert isinstance(calls[0]["kwargs"]["cutoff"], str)


@pytest.mark.asyncio
async def test_prune_stub_orphans_returns_zero_when_nothing_matches(monkeypatch):
    """When no rows match the predicate, ``count(e)`` is 0 and the
    function returns 0 — not None."""
    store, _calls, set_single = _make_neo4j_store_with_capture()
    set_single({"n": 0})

    purged = await store.prune_stub_orphans(ttl_hours=24)
    assert purged == 0


# ── 4. upsert_wiki_reference_entity_edge idempotency ──────────────────────


@pytest.mark.asyncio
async def test_upsert_wiki_reference_entity_edge_idempotent(monkeypatch):
    """Calling the entity-edge writer twice produces the same Cypher
    (MERGE) so the second invocation does not create a duplicate edge.
    Both invocations land identical kwargs.
    """
    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single(None)

    await store.upsert_wiki_reference_entity_edge(
        channel_id="C-test",
        target_lang="en",
        src_slug="topic:auth",
        entity_name="Alice",
    )
    await store.upsert_wiki_reference_entity_edge(
        channel_id="C-test",
        target_lang="en",
        src_slug="topic:auth",
        entity_name="Alice",
    )

    assert len(calls) == 2
    # Same Cypher for both calls.
    assert calls[0]["query"] == calls[1]["query"]
    # Same kwargs.
    assert calls[0]["kwargs"] == calls[1]["kwargs"]
    # MERGE is used for the edge (idempotency in Neo4j's terms).
    query = calls[0]["query"]
    assert "MERGE (src)-[:REFERENCES_ENTITY]->(e)" in query
    # WikiPage is MATCHed (must exist), Entity is MATCHed (no stub creation).
    assert (
        "MATCH (src:WikiPage {channel_id: $channel_id, target_lang: $target_lang, slug: $src_slug})"
        in query
    )
    assert "MATCH (e:Entity {name: $entity_name})" in query


# ── 5. delete_channel_wiki_graph ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_channel_wiki_graph_returns_count():
    """``delete_channel_wiki_graph`` issues a DETACH DELETE on WikiPage
    nodes filtered by channel_id and returns the count from the single
    record.
    """
    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"n": 7})

    deleted = await store.delete_channel_wiki_graph("C-reset")

    assert deleted == 7
    assert len(calls) == 1
    query = calls[0]["query"]
    assert "WikiPage" in query
    assert "DETACH DELETE" in query
    assert calls[0]["kwargs"] == {"channel_id": "C-reset"}


@pytest.mark.asyncio
async def test_delete_channel_wiki_graph_returns_zero_when_empty():
    """When no rows match, ``count(w)=0`` and the method returns 0
    (not None) so callers can sum counts without conditional logic.
    """
    store, _calls, set_single = _make_neo4j_store_with_capture()
    set_single({"n": 0})

    deleted = await store.delete_channel_wiki_graph("C-empty")
    assert deleted == 0


@pytest.mark.asyncio
async def test_delete_channel_wiki_graph_handles_missing_record():
    """``result.single()`` may return None on an empty cursor; the
    method must still return 0 rather than blow up.
    """
    store, _calls, set_single = _make_neo4j_store_with_capture()
    set_single(None)

    deleted = await store.delete_channel_wiki_graph("C-no-record")
    assert deleted == 0
