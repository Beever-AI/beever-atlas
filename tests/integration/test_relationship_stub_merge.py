"""Integration tests for PR-2 MATCH→MERGE stub-endpoint creation.

Covers Task 1 + Task 2 acceptance criteria from
``.omc/plans/pipeline-realign-v2.md`` that can be verified against a
mocked Neo4j driver:

* unknown endpoints → 2 stub Entity nodes (criterion #3)
* known endpoints → ON CREATE does not rewrite (criterion #4)
* episodic_link unknown name → stub with reason=episodic_link (Task 2 #1)
* stub-explosion cap fires at >50 (criterion #6)
* persister post-hoc stub type is Topic (criterion #7 regression)

The "concurrent_upsert" criterion #5 requires a real Neo4j container
(the UNIQUE constraint must serialise MERGE) and is left as
manual-test-only.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.models.domain import GraphRelationship


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_neo4j_store_with_capture():
    """Build a Neo4jStore with a mocked async driver and record every
    ``session.run`` call (query, kwargs).

    Returns ``(store, calls, set_single_result)`` where ``calls`` is a
    list that grows with every Cypher invocation and ``set_single_result``
    is a callable that controls what ``result.single()`` returns for the
    next ``session.run``.
    """
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)
    calls: list[dict] = []
    next_single = {"value": None}

    mock_result = MagicMock()

    async def _single():
        return next_single["value"]

    async def _data():
        return next_single["value"] or []

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
    """Force ``Settings.neo4j_relationship_stub_endpoints`` for the duration
    of a test, bypassing the lru_cache on ``get_settings()``.
    """
    from beever_atlas.infra import config as _config_mod

    real_settings = _config_mod.get_settings()
    object.__setattr__(real_settings, "neo4j_relationship_stub_endpoints", stub_endpoints)
    # Yield path: leave the field flipped — the registry is process-local
    # for the test and pytest re-imports Settings between sessions.


# ── Test 1 — unknown endpoints create Unresolved stubs ────────────────────


@pytest.mark.asyncio
async def test_unknown_endpoints_create_unresolved_stubs(monkeypatch):
    """MERGE path: relationship with two unknown endpoint names creates 2
    stub Entity nodes (type='Unresolved', scope='global'). Verified by:
    (a) the Cypher contains the stub-MERGE clause; (b) the stub_props
    parameter contains ``"stub": true, "reason": "rel_endpoint",
    "awaiting_type": true``.
    """
    _force_settings(monkeypatch, stub_endpoints=True)

    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"eid": "rel-1", "stubs_created": 2})

    rel = GraphRelationship(
        source="UnknownA",
        target="UnknownB",
        type="DEPENDS_ON",
        confidence=0.9,
    )
    eid, stub_count = await store._upsert_relationship_with_stub_flag(rel)

    assert eid == "rel-1"
    assert stub_count == 2
    assert len(calls) == 1
    query = calls[0]["query"]
    kwargs = calls[0]["kwargs"]
    # Cypher uses MERGE for both endpoints with composite key.
    # Variables are ``a_raw`` / ``b_raw`` so the apoc.refactor.mergeNodes
    # absorption step (symmetric heal) can replace them with the typed
    # sibling without aliasing the relationship endpoints.
    assert "MERGE (a_raw:Entity {name: $source, type: 'Unresolved', scope: 'global'})" in query
    assert "MERGE (b_raw:Entity {name: $target, type: 'Unresolved', scope: 'global'})" in query
    # ON CREATE SET writes the stub marker.
    assert "ON CREATE SET" in query
    assert kwargs["stub_props"] == '{"stub": true, "reason": "rel_endpoint", "awaiting_type": true}'
    assert kwargs["source"] == "UnknownA"
    assert kwargs["target"] == "UnknownB"


# ── Test 2 — known endpoints unchanged (ON CREATE only) ────────────────────


@pytest.mark.asyncio
async def test_known_endpoints_unchanged(monkeypatch):
    """When the endpoint Entity already exists at ``(name, 'Unresolved',
    'global')`` (i.e. real, not stub), ``ON CREATE SET`` does NOT fire
    so the existing properties remain intact. The Cypher we issue is
    the same; we assert the structure here and rely on the integration
    semantics of MERGE for the rewrite-prevention guarantee.

    The composite-key design intentionally creates a NEW
    ``(name, 'Unresolved', 'global')`` stub when a real entity exists
    with a richer type like ``Tool`` — the heal-path in
    :meth:`Neo4jStore.upsert_entity` collapses the two on the next
    typed write.
    """
    _force_settings(monkeypatch, stub_endpoints=True)

    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"eid": "rel-2", "stubs_created": 0})

    rel = GraphRelationship(
        source="KnownTool",
        target="OtherTool",
        type="INTEGRATES_WITH",
        confidence=0.9,
    )
    eid, stub_count = await store._upsert_relationship_with_stub_flag(rel)

    assert eid == "rel-2"
    # Both endpoints already exist at the composite key — no new stubs.
    assert stub_count == 0
    query = calls[0]["query"]
    # ON CREATE SET is gated by MERGE — it does not run for matched nodes.
    assert "ON CREATE SET" in query
    # The properties/aliases sets are NOT inside an ON MATCH SET clause
    # (we only write on creation), which is what preserves Tool-typed
    # known entities sharing the same name.
    assert "ON MATCH SET" not in query.split("CALL apoc.merge.relationship")[0]


# ── Test 3 — batch_create_episodic_links MERGEs unknown entity ────────────


@pytest.mark.asyncio
async def test_episodic_link_unknown_entity_creates_stub(monkeypatch):
    """``batch_create_episodic_links`` with an unknown entity_name uses
    MERGE on the Entity node, tagging the stub with
    ``reason='episodic_link'``.
    """
    _force_settings(monkeypatch, stub_endpoints=True)

    store, calls, set_single = _make_neo4j_store_with_capture()
    set_single({"created": 1})

    links = [
        {
            "entity_name": "UnknownEpisodic",
            "weaviate_fact_id": "fact-1",
            "message_ts": "1234567890.0001",
            "channel_id": "C123",
        }
    ]
    created = await store.batch_create_episodic_links(links)

    assert created == 1
    assert len(calls) == 1
    query = calls[0]["query"]
    # MERGE the Entity (not MATCH) with the composite key. Variable is
    # ``e_raw`` so the apoc.refactor.mergeNodes absorption step can
    # replace it with the typed sibling for the MENTIONED_IN edge.
    assert (
        "MERGE (e_raw:Entity {name: link.entity_name, type: 'Unresolved', scope: 'global'})"
        in query
    )
    assert '"reason": "episodic_link"' in query
    assert '"awaiting_type": true' in query
    # Event MERGE still present.
    assert "MERGE (ep:Event {weaviate_id: link.weaviate_fact_id})" in query
    assert "MERGE (e)-[:MENTIONED_IN]->(ep)" in query


# ── Test 4 — stub-explosion cap (>50 stubs/batch) ─────────────────────────


@pytest.mark.asyncio
async def test_stub_explosion_cap(monkeypatch, caplog):
    """Batch with 51 unknown-endpoint relationships triggers the
    fail-closed cap: ERROR log + ``stub_explosion_detected`` metric.
    The batch still commits (pollution, not fatal).
    """
    _force_settings(monkeypatch, stub_endpoints=True)
    from beever_atlas.stores.neo4j_store import Neo4jStore

    store = Neo4jStore.__new__(Neo4jStore)

    # Each fake upsert reports 1 stub created (the source side). 51 calls
    # → 51 stubs → exceeds the default _STUB_EXPLOSION_THRESHOLD (50).
    async def fake_upsert(rel):
        return f"rel-{rel.source}", 1

    monkeypatch.setattr(store, "_upsert_relationship_with_stub_flag", fake_upsert)

    rels = [
        GraphRelationship(
            source=f"src-{i}",
            target=f"tgt-{i}",
            type="MENTIONS",
            confidence=0.5,
        )
        for i in range(51)
    ]

    # Capture the metric increment.
    captured_metric: dict[str, object] = {}

    def fake_increment(channel_id, sync_job_id, metric, delta=1):
        captured_metric.update(
            channel_id=channel_id,
            sync_job_id=sync_job_id,
            metric=metric,
            delta=delta,
        )

    monkeypatch.setattr(
        "beever_atlas.services.batch_processor.increment_sync_metric",
        fake_increment,
    )

    # Capture ERROR log via direct monkeypatch on the module logger
    # (the project-wide caplog plumbing is unreliable per existing
    # patterns in test_neo4j_batch_bounded.py).
    import beever_atlas.stores.neo4j_store as neo4j_mod

    errors: list[str] = []
    monkeypatch.setattr(
        neo4j_mod.logger,
        "error",
        lambda msg, *a, **kw: errors.append(msg % a if a else msg),
    )

    ids = await store.batch_upsert_relationships(
        rels,
        channel_id="C-cap",
        sync_job_id="job-cap",
        batch_idx=7,
    )

    # All 51 relationships still committed.
    assert len(ids) == 51
    assert all(eid.startswith("rel-") for eid in ids)

    # ERROR log fired with the stub count.
    assert errors, "expected ERROR log on stub explosion"
    assert any("stub explosion detected" in m for m in errors)
    assert any("51" in m for m in errors)

    # Metric incremented with channel + sync_job + count.
    assert captured_metric == {
        "channel_id": "C-cap",
        "sync_job_id": "job-cap",
        "metric": "stub_explosion_detected",
        "delta": 51,
    }


# ── Test 5 — persister post-hoc stub uses Unresolved ──────────────────────


def test_persister_post_hoc_stub_type_is_unresolved():
    """Regression test: the post-hoc stub block in the persister must
    produce ``type='Unresolved'`` so it aligns with the in-Cypher MERGE
    default and can be healed by the ``upsert_entity`` heal-path. The
    literal ``type="Topic"`` must NOT appear in the post-hoc stub block
    (it would mismatch the in-Cypher MERGE default and leak Topic
    monoculture back into the graph).
    """
    from pathlib import Path

    src = Path("src/beever_atlas/agents/ingestion/persister.py").read_text(encoding="utf-8")
    assert 'type="Project"' not in src, (
        'persister.py still constructs a stub with type="Project" — '
        'must be "Unresolved" to align with the in-Cypher MERGE default.'
    )
    assert 'type="Topic"' not in src, (
        'persister.py still constructs a stub with type="Topic" — '
        'must be "Unresolved" to align with the in-Cypher MERGE default.'
    )
    # Positive assertion: the post-hoc loop now uses Unresolved.
    assert 'type="Unresolved"' in src, (
        'expected the post-hoc stub block to construct GraphEntity(type="Unresolved", ...)'
    )


# ── Test 6 — env-flag false reverts to legacy MATCH ───────────────────────


@pytest.mark.asyncio
async def test_legacy_match_path_when_flag_false(monkeypatch):
    """When ``NEO4J_RELATIONSHIP_STUB_ENDPOINTS=false``, the Cypher uses
    the legacy ``MATCH (a:Entity {name: $source})`` pattern and returns
    ``("", 0)`` when MATCH yields no row.
    """
    _force_settings(monkeypatch, stub_endpoints=False)

    store, calls, set_single = _make_neo4j_store_with_capture()
    # Legacy MATCH-and-skip: single() returns None when endpoints missing.
    set_single(None)

    rel = GraphRelationship(
        source="MissingA",
        target="MissingB",
        type="USES",
        confidence=0.7,
    )
    eid, stub_count = await store._upsert_relationship_with_stub_flag(rel)

    assert eid == ""
    assert stub_count == 0
    query = calls[0]["query"]
    # Legacy uses MATCH, not MERGE on the endpoints.
    assert "MATCH (a:Entity {name: $source})" in query
    assert "MATCH (b:Entity {name: $target})" in query
    assert "ON CREATE SET" not in query

    # Reset flag for other tests in the session.
    _force_settings(monkeypatch, stub_endpoints=True)
