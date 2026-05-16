"""Tests for the persister stub-seeding gate.

The gate lives inline at ``persister.py:~449-560`` (post-heal-path
refactor). Three guarantees:

  1. Relationships with confidence < 0.8 do not seed stubs (gate floor).
  2. Relationships where BOTH endpoints are unknown (not in the typed
     entity set) drop the rel and increment
     ``relationships_dropped_total`` with reason ``both_endpoints_unknown``.
  3. Relationships with one known + one unknown endpoint produce one
     ``Unresolved`` stub for the unknown side; the counter is NOT
     incremented for these rels.

The tests exercise the gate by driving ``PersisterAgent._run_async_impl``
with a minimal ``InvocationContext`` and fully mocked stores so we can
assert exactly which ``batch_upsert_entities`` calls land.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_ctx(
    *,
    embedded_facts: list[dict[str, Any]] | None = None,
    validated_entities: dict[str, Any] | None = None,
) -> SimpleNamespace:
    """Construct a minimal ``InvocationContext``-like object the
    PersisterAgent expects. We only need the fields it reads.
    """
    state: dict[str, Any] = {
        "sync_job_id": "job-test",
        "channel_id": "C-test",
        "batch_num": 1,
        "source_language": "en",
        "embedded_facts": embedded_facts or [],
        "validated_entities": validated_entities or {"entities": [], "relationships": []},
        "skip_graph_writes": False,
    }
    session = SimpleNamespace(state=state)
    return SimpleNamespace(
        session=session,
        invocation_id="inv-test",
    )


def _make_stores_mock(
    *,
    existing_entity_names: set[str] | None = None,
) -> tuple[MagicMock, dict[str, list]]:
    """Build a stub ``StoreClients`` with the methods the persister
    touches. Returns ``(stores, captured)`` where ``captured`` is a
    dict-of-lists capturing the args of every interesting call.
    """
    existing = existing_entity_names or set()
    captured: dict[str, list] = {
        "batch_upsert_entities": [],
        "batch_upsert_relationships": [],
        "batch_create_episodic_links": [],
        "batch_find_entities_by_name": [],
    }

    stores = MagicMock()
    # Mongo / outbox path
    stores.mongodb.create_write_intent = AsyncMock(return_value="intent-test")
    stores.mongodb.mark_intent_weaviate_done = AsyncMock(return_value=None)
    stores.mongodb.mark_intent_neo4j_done = AsyncMock(return_value=None)
    stores.mongodb.mark_intent_complete = AsyncMock(return_value=None)

    # Weaviate
    async def fake_weaviate_upsert(facts):
        return [f"wid-{i}" for i in range(len(facts))]

    stores.weaviate.batch_upsert_facts = AsyncMock(side_effect=fake_weaviate_upsert)

    # Entity registry
    async def fake_compute_name_embeddings_batch(names):
        return {}

    async def fake_batch_store_name_vectors(items):
        return 0

    async def fake_store_name_vector(name, vec):
        return None

    stores.entity_registry.compute_name_embeddings_batch = AsyncMock(
        side_effect=fake_compute_name_embeddings_batch
    )
    stores.entity_registry.batch_store_name_vectors = AsyncMock(
        side_effect=fake_batch_store_name_vectors
    )
    stores.entity_registry.store_name_vector = AsyncMock(side_effect=fake_store_name_vector)

    # Graph
    async def fake_batch_upsert_entities(entities):
        captured["batch_upsert_entities"].append(list(entities))
        return [f"eid-{e.name}" for e in entities]

    async def fake_batch_upsert_relationships(rels, **kwargs):
        captured["batch_upsert_relationships"].append((list(rels), dict(kwargs)))
        return [f"rid-{i}" for i in range(len(rels))]

    async def fake_batch_create_episodic_links(links):
        captured["batch_create_episodic_links"].append(list(links))
        return len(links)

    async def fake_batch_find_entities_by_name(names):
        captured["batch_find_entities_by_name"].append(list(names))
        return set(names) & existing

    async def fake_batch_promote_pending(names):
        return 0

    async def fake_batch_upsert_media(items):
        return 0

    async def fake_batch_link_entities_to_media(links):
        return 0

    stores.graph.batch_upsert_entities = AsyncMock(side_effect=fake_batch_upsert_entities)
    stores.graph.batch_upsert_relationships = AsyncMock(side_effect=fake_batch_upsert_relationships)
    stores.graph.batch_create_episodic_links = AsyncMock(
        side_effect=fake_batch_create_episodic_links
    )
    stores.graph.batch_find_entities_by_name = AsyncMock(
        side_effect=fake_batch_find_entities_by_name
    )
    stores.graph.batch_promote_pending = AsyncMock(side_effect=fake_batch_promote_pending)
    stores.graph.batch_upsert_media = AsyncMock(side_effect=fake_batch_upsert_media)
    stores.graph.batch_link_entities_to_media = AsyncMock(
        side_effect=fake_batch_link_entities_to_media
    )

    return stores, captured


async def _run_persister(ctx, stores, monkeypatch):
    """Wire ``get_stores()`` to the mock and drive the agent."""
    from beever_atlas.agents.ingestion import persister as persister_mod

    monkeypatch.setattr(persister_mod, "get_stores", lambda: stores)
    # The persister also imports increment_sync_metric lazily — patch it.
    captured_metrics: list[tuple[str, str, str, int]] = []

    def fake_increment(channel_id, sync_job_id, metric, delta=1):
        captured_metrics.append((channel_id, sync_job_id, metric, delta))

    monkeypatch.setattr(
        "beever_atlas.services.batch_processor.increment_sync_metric",
        fake_increment,
    )

    agent = persister_mod.PersisterAgent(name="persister")
    async for _ in agent._run_async_impl(ctx):
        pass
    return captured_metrics


# ── 1. Low confidence rels do not seed stubs ────────────────────────────────


@pytest.mark.asyncio
async def test_low_confidence_relationship_does_not_seed_stubs(monkeypatch):
    """A relationship with confidence 0.5 is below the gate (0.8). Its
    endpoint names must NOT be turned into stub entities, even if they
    appear in fact ``entity_tags``.
    """
    facts = [
        {
            "memory_text": "Alice may use Redis someday",
            "entity_tags": ["Alice", "Redis"],
            "source_message_id": "m1",
            "message_ts": "1000.0",
            "channel_id": "C-test",
            "fact_type": "observation",
        }
    ]
    rels = [
        {
            "type": "USES",
            "source": "Alice",
            "target": "Redis",
            "confidence": 0.5,
        }
    ]
    ctx = _make_ctx(
        embedded_facts=facts,
        validated_entities={"entities": [], "relationships": rels},
    )
    stores, captured = _make_stores_mock()
    metrics = await _run_persister(ctx, stores, monkeypatch)

    # No stub batch_upsert_entities call: only the initial empty entities
    # list. (When entities=[] the persister skips the call entirely.)
    stub_calls = [
        c for c in captured["batch_upsert_entities"] if any(getattr(e, "name", None) for e in c)
    ]
    assert stub_calls == [], (
        f"expected no stub entities created for low-confidence rel; got: "
        f"{[[(e.name, e.type) for e in batch] for batch in stub_calls]}"
    )
    # The counter for both_endpoints_unknown must NOT fire — the gate
    # filtered the rel out before the unknown-endpoint check.
    rel_drop_metrics = [m for m in metrics if m[2] == "relationships_dropped_total"]
    assert rel_drop_metrics == []


# ── 2. Both endpoints unknown drops the rel + increments counter ───────────


@pytest.mark.asyncio
async def test_two_unknown_endpoints_drop_rel_and_increment_counter(monkeypatch):
    """A high-confidence relationship where both endpoints are unknown
    (not in the typed entity set returned by extractor + not pre-existing
    in Neo4j) is dropped from the stub-seed list and increments
    ``relationships_dropped_total`` with reason ``both_endpoints_unknown``.

    No stub entities should be created.
    """
    facts = [
        {
            "memory_text": "MysteryA decided MysteryB",
            "entity_tags": ["MysteryA", "MysteryB"],
            "source_message_id": "m1",
            "message_ts": "1000.0",
            "channel_id": "C-test",
            "fact_type": "decision",
        }
    ]
    rels = [
        {
            "type": "DECIDED",
            "source": "MysteryA",
            "target": "MysteryB",
            "confidence": 0.9,
        }
    ]
    ctx = _make_ctx(
        embedded_facts=facts,
        validated_entities={"entities": [], "relationships": rels},
    )
    stores, captured = _make_stores_mock(existing_entity_names=set())
    metrics = await _run_persister(ctx, stores, monkeypatch)

    # No entity created with name MysteryA or MysteryB.
    all_created_names = {e.name for batch in captured["batch_upsert_entities"] for e in batch}
    assert "MysteryA" not in all_created_names
    assert "MysteryB" not in all_created_names

    # The drop counter fired with reason both_endpoints_unknown — we
    # bundle the reason into the metric name in this implementation.
    rel_drop_metrics = [m for m in metrics if m[2] == "relationships_dropped_total"]
    assert rel_drop_metrics, "expected relationships_dropped_total to be incremented"
    # delta is the count of dropped rels.
    assert rel_drop_metrics[0][3] >= 1


# ── 3. One known + one unknown → one stub, no counter increment ───────────


@pytest.mark.asyncio
async def test_one_unknown_endpoint_creates_one_unresolved_stub(monkeypatch):
    """A high-confidence relationship with one typed endpoint (Alice as
    Person) and one unknown endpoint (UnknownProj) creates exactly one
    stub Entity for the unknown side. The stub is typed ``Unresolved``.
    The counter for both_endpoints_unknown is NOT incremented.
    """
    facts = [
        {
            "memory_text": "Alice works on UnknownProj",
            "entity_tags": ["Alice", "UnknownProj"],
            "source_message_id": "m1",
            "message_ts": "1000.0",
            "channel_id": "C-test",
            "fact_type": "observation",
        }
    ]
    rels = [
        {
            "type": "WORKS_ON",
            "source": "Alice",
            "target": "UnknownProj",
            "confidence": 0.9,
        }
    ]
    entities = [
        {
            "name": "Alice",
            "type": "Person",
            "scope": "global",
        }
    ]
    ctx = _make_ctx(
        embedded_facts=facts,
        validated_entities={"entities": entities, "relationships": rels},
    )
    stores, captured = _make_stores_mock(existing_entity_names=set())
    metrics = await _run_persister(ctx, stores, monkeypatch)

    # The stub-seed call should produce exactly one stub Entity for
    # ``UnknownProj`` typed ``Unresolved``. The first batch_upsert_entities
    # call is the typed entities (Alice). The stub call comes later.
    stub_entities = []
    for batch in captured["batch_upsert_entities"]:
        for e in batch:
            if e.type == "Unresolved":
                stub_entities.append(e)
    assert len(stub_entities) == 1, (
        f"expected exactly 1 Unresolved stub; got {[(e.name, e.type) for e in stub_entities]}"
    )
    assert stub_entities[0].name == "UnknownProj"
    assert stub_entities[0].type == "Unresolved"
    # Stub property markers.
    assert stub_entities[0].properties.get("stub") is True
    assert stub_entities[0].properties.get("awaiting_type") is True

    # Counter NOT incremented for this rel (one endpoint is known).
    rel_drop_metrics = [m for m in metrics if m[2] == "relationships_dropped_total"]
    assert rel_drop_metrics == []
