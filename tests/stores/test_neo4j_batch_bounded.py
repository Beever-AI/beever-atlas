"""Tests for `Neo4jStore.batch_upsert_*` bounded concurrency + partial-failure
tolerance (issue #37).

Covers:
  * (a/d) bounded concurrency — peak ≤ semaphore limit
  * (b/e) partial-failure tolerance — one failing entity/relationship
    does not poison the batch
  * (c/f) empty input — short-circuits without invoking inner upsert
"""

from __future__ import annotations

import asyncio

from beever_atlas.models.domain import GraphEntity, GraphRelationship
from beever_atlas.stores.neo4j_store import Neo4jStore


def _make_entity(name: str) -> GraphEntity:
    return GraphEntity(name=name, type="Person")


def _make_relationship(source: str, target: str) -> GraphRelationship:
    return GraphRelationship(source=source, target=target, type="KNOWS", confidence=0.9)


# ── Bounded concurrency (a, d) ──────────────────────────────────────────


async def test_batch_upsert_entities_bounded_concurrency(monkeypatch) -> None:
    """Peak concurrent `upsert_entity` invocations must not exceed
    `_BATCH_CONCURRENCY`. Use a tight limit (4) for fast, deterministic
    testing; mock keeps tasks alive briefly so the semaphore actually
    blocks (per existing patterns in test_image_extractor_concurrency)."""
    monkeypatch.setattr(Neo4jStore, "_BATCH_CONCURRENCY", 4)

    store = Neo4jStore.__new__(Neo4jStore)  # bypass __init__ — no driver
    inflight = 0
    peak = 0

    async def fake_upsert(entity: GraphEntity) -> str:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)  # keep concurrent tasks alive
        inflight -= 1
        return f"eid-{entity.name}"

    monkeypatch.setattr(store, "upsert_entity", fake_upsert)

    entities = [_make_entity(f"e{i}") for i in range(40)]
    ids = await store.batch_upsert_entities(entities)

    assert peak <= 4, f"peak {peak} exceeded _BATCH_CONCURRENCY=4"
    assert len(ids) == 40
    assert all(i.startswith("eid-e") for i in ids)


async def test_batch_upsert_relationships_bounded_concurrency(monkeypatch) -> None:
    monkeypatch.setattr(Neo4jStore, "_BATCH_CONCURRENCY", 4)

    store = Neo4jStore.__new__(Neo4jStore)
    inflight = 0
    peak = 0

    async def fake_upsert(rel: GraphRelationship) -> str:
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.01)
        inflight -= 1
        return f"rel-{rel.source}-{rel.target}"

    monkeypatch.setattr(store, "upsert_relationship", fake_upsert)

    rels = [_make_relationship(f"a{i}", f"b{i}") for i in range(40)]
    ids = await store.batch_upsert_relationships(rels)

    assert peak <= 4, f"peak {peak} exceeded _BATCH_CONCURRENCY=4"
    assert len(ids) == 40


# ── Partial failure tolerance (b, e) ────────────────────────────────────


async def test_batch_upsert_entities_partial_failure(monkeypatch) -> None:
    """One failing entity must not crash the batch. Failed slot is `""`
    (empty-string sentinel matching `batch_upsert_relationships`)."""
    store = Neo4jStore.__new__(Neo4jStore)

    async def fake_upsert(entity: GraphEntity) -> str:
        if entity.name == "e1":
            raise RuntimeError("boom")
        return f"eid-{entity.name}"

    monkeypatch.setattr(store, "upsert_entity", fake_upsert)

    entities = [_make_entity(f"e{i}") for i in range(3)]
    ids = await store.batch_upsert_entities(entities)

    assert ids == ["eid-e0", "", "eid-e2"]


async def test_batch_upsert_relationships_partial_failure(monkeypatch) -> None:
    store = Neo4jStore.__new__(Neo4jStore)

    async def fake_upsert(rel: GraphRelationship) -> str:
        if rel.source == "a1":
            raise RuntimeError("boom")
        return f"rel-{rel.source}"

    monkeypatch.setattr(store, "upsert_relationship", fake_upsert)

    rels = [_make_relationship(f"a{i}", f"b{i}") for i in range(3)]
    ids = await store.batch_upsert_relationships(rels)

    assert ids == ["rel-a0", "", "rel-a2"]


# ── Empty input short-circuit (c, f) ────────────────────────────────────


async def test_batch_upsert_entities_empty_input(monkeypatch) -> None:
    """Empty entity list returns `[]` without invoking `upsert_entity`."""
    store = Neo4jStore.__new__(Neo4jStore)
    call_count = 0

    async def fake_upsert(_entity: GraphEntity) -> str:
        nonlocal call_count
        call_count += 1
        return ""

    monkeypatch.setattr(store, "upsert_entity", fake_upsert)

    result = await store.batch_upsert_entities([])

    assert result == []
    assert call_count == 0


async def test_batch_upsert_relationships_empty_input(monkeypatch) -> None:
    store = Neo4jStore.__new__(Neo4jStore)
    call_count = 0

    async def fake_upsert(_rel: GraphRelationship) -> str:
        nonlocal call_count
        call_count += 1
        return ""

    monkeypatch.setattr(store, "upsert_relationship", fake_upsert)

    result = await store.batch_upsert_relationships([])

    assert result == []
    assert call_count == 0


# ── Failure logs entity name (b regression for log content) ─────────────


async def test_batch_upsert_entities_logs_entity_name_on_failure(monkeypatch) -> None:
    """On failure, the WARNING log must include the failing entity's name
    so operators can correlate. caplog can't catch records from
    `beever_atlas.*` loggers in this project (propagation disabled at the
    server-app level by autouse `_auth_bypass` fixture). Capture via
    direct monkeypatch on the module's logger instead."""
    import beever_atlas.stores.neo4j_store as neo4j_mod

    captured: list[str] = []
    monkeypatch.setattr(
        neo4j_mod.logger,
        "warning",
        lambda msg, *a, **kw: captured.append(msg % a if a else msg),
    )

    store = Neo4jStore.__new__(Neo4jStore)

    async def fake_upsert(_entity: GraphEntity) -> str:
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "upsert_entity", fake_upsert)

    entities = [_make_entity("alice")]
    ids = await store.batch_upsert_entities(entities)

    assert ids == [""]
    assert any("alice" in m for m in captured), f"expected 'alice' in log; got {captured}"
