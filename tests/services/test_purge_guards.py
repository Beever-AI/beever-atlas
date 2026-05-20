"""Writer-guard + cancel-helper tests for the channel hard-purge (Wave 0).

delete-channel-v2 Wave 0. Verifies the five writer guards refuse/skip when a
non-stale purge lock is held, the extraction CLAIM filter excludes purging
ids, and the process-local cancel helpers cancel an in-flight task.

These exercise the guard *seams* with lightweight mocks rather than live
infra — full end-to-end resurrection coverage (AC#1) lands in Wave 5
integration against real Mongo/Weaviate/graph.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _stores_with_purging(purging: set[str], *, is_purging: bool | None = None) -> SimpleNamespace:
    """Build a SimpleNamespace stores stub whose mongodb reports purge state."""
    mongodb = SimpleNamespace()
    mongodb.get_purging_channel_ids = AsyncMock(return_value=set(purging))
    mongodb.is_purging = AsyncMock(
        return_value=(is_purging if is_purging is not None else bool(purging))
    )
    return SimpleNamespace(mongodb=mongodb)


# ─────────────────────────────────────────────────────────────────────────────
# Guard 1: SyncRunner.start_sync refuses a purging channel
# ─────────────────────────────────────────────────────────────────────────────


async def test_start_sync_raises_when_purging() -> None:
    from beever_atlas.services.sync_runner import SyncRunner

    runner = SyncRunner()
    stores = _stores_with_purging({"C1"}, is_purging=True)
    with (
        patch("beever_atlas.services.sync_runner.get_stores", return_value=stores),
        patch("beever_atlas.services.sync_runner.get_settings", return_value=MagicMock()),
    ):
        with pytest.raises(ValueError, match="purged"):
            await runner.start_sync("C1")
    # The guard must fire BEFORE the stale-job recovery body — so is_purging
    # was consulted and we never reached get_sync_status.
    stores.mongodb.is_purging.assert_awaited_once_with("C1")


async def test_start_sync_not_blocked_when_not_purging() -> None:
    """When not purging, the guard lets execution proceed (and fails later on
    the missing stale-recovery stubs, proving the guard did not short-circuit)."""
    from beever_atlas.services.sync_runner import SyncRunner

    runner = SyncRunner()
    stores = _stores_with_purging(set(), is_purging=False)
    with (
        patch("beever_atlas.services.sync_runner.get_stores", return_value=stores),
        patch("beever_atlas.services.sync_runner.get_settings", return_value=MagicMock()),
    ):
        # We don't stub the full sync path; assert only that the purge guard
        # did NOT raise the purge ValueError. Any *other* error is fine here.
        try:
            await runner.start_sync("C1")
        except ValueError as exc:
            assert "purged" not in str(exc)
        except Exception:
            pass
    stores.mongodb.is_purging.assert_awaited_once_with("C1")


# ─────────────────────────────────────────────────────────────────────────────
# Guard 2: pipeline_orchestrator._spawn_consolidation skips a purging channel
# ─────────────────────────────────────────────────────────────────────────────


async def test_spawn_consolidation_skips_purging() -> None:
    from beever_atlas.services import pipeline_orchestrator as orch

    orch._consolidation_tasks.clear()
    orch._purging_snapshot = {"C1"}
    try:
        orch._spawn_consolidation("C1")
        assert "C1" not in orch._consolidation_tasks
    finally:
        orch._purging_snapshot = set()
        orch._consolidation_tasks.clear()


async def test_refresh_purging_snapshot_populates_set() -> None:
    from beever_atlas.services import pipeline_orchestrator as orch

    stores = _stores_with_purging({"C9"})
    with patch.object(orch, "get_stores", return_value=stores):
        await orch._refresh_purging_snapshot()
    try:
        assert orch._purging_snapshot == {"C9"}
    finally:
        orch._purging_snapshot = set()


# ─────────────────────────────────────────────────────────────────────────────
# Guard 3: WriteReconciler._reconcile_intent drops purging-channel facts only
# ─────────────────────────────────────────────────────────────────────────────


async def test_reconciler_drops_purging_channel_facts_only() -> None:
    from beever_atlas.services.reconciler import WriteReconciler

    # Mixed-channel intent: facts for purging X and active Y; entities for both.
    wi = SimpleNamespace(
        id="intent-1",
        weaviate_done=False,
        neo4j_done=False,
        facts=[
            {"memory_text": "x fact", "entity_tags": ["a"], "channel_id": "X"},
            {"memory_text": "y fact", "entity_tags": ["b"], "channel_id": "Y"},
        ],
        entities=[
            {"name": "EX", "type": "person", "channel_id": "X"},
            {"name": "EY", "type": "person", "channel_id": "Y"},
        ],
        relationships=[],
    )

    weaviate = SimpleNamespace(batch_upsert_facts=AsyncMock())
    graph = SimpleNamespace(
        batch_upsert_entities=AsyncMock(),
        batch_upsert_relationships=AsyncMock(),
    )
    mongodb = SimpleNamespace(
        get_purging_channel_ids=AsyncMock(return_value={"X"}),
        mark_intent_weaviate_done=AsyncMock(),
        mark_intent_neo4j_done=AsyncMock(),
        mark_intent_complete=AsyncMock(),
    )
    stores = SimpleNamespace(weaviate=weaviate, graph=graph, mongodb=mongodb)

    rec = WriteReconciler()
    await rec._reconcile_intent("intent-1", wi, stores)

    # Only Y's fact survived the per-fact filter.
    weaviate.batch_upsert_facts.assert_awaited_once()
    upserted_facts = weaviate.batch_upsert_facts.await_args.args[0]
    assert len(upserted_facts) == 1
    assert upserted_facts[0].channel_id == "Y"

    # Only Y's entity survived.
    graph.batch_upsert_entities.assert_awaited_once()
    upserted_entities = graph.batch_upsert_entities.await_args.args[0]
    assert len(upserted_entities) == 1

    # Intent is still marked complete (no livelock).
    mongodb.mark_intent_complete.assert_awaited_once_with("intent-1")


async def test_reconciler_replays_all_when_nothing_purging() -> None:
    from beever_atlas.services.reconciler import WriteReconciler

    wi = SimpleNamespace(
        id="intent-2",
        weaviate_done=False,
        neo4j_done=True,
        facts=[
            {"memory_text": "x fact", "entity_tags": ["a"], "channel_id": "X"},
            {"memory_text": "y fact", "entity_tags": ["b"], "channel_id": "Y"},
        ],
        entities=[],
        relationships=[],
    )
    weaviate = SimpleNamespace(batch_upsert_facts=AsyncMock())
    mongodb = SimpleNamespace(
        get_purging_channel_ids=AsyncMock(return_value=set()),
        mark_intent_weaviate_done=AsyncMock(),
        mark_intent_neo4j_done=AsyncMock(),
        mark_intent_complete=AsyncMock(),
    )
    stores = SimpleNamespace(
        weaviate=weaviate,
        graph=SimpleNamespace(),
        mongodb=mongodb,
    )
    rec = WriteReconciler()
    await rec._reconcile_intent("intent-2", wi, stores)
    upserted_facts = weaviate.batch_upsert_facts.await_args.args[0]
    assert len(upserted_facts) == 2


# ─────────────────────────────────────────────────────────────────────────────
# Guard 4: extraction CLAIM filter excludes purging ids (global tick path)
# ─────────────────────────────────────────────────────────────────────────────


async def test_extraction_claim_filter_excludes_purging_ids() -> None:
    """The global drain (channel_id=None) must add a ``$nin`` over purging ids."""
    from beever_atlas.stores.mongodb_store import MongoDBStore

    store = MongoDBStore.__new__(MongoDBStore)
    captured: dict[str, object] = {}

    async def _fake_fou(filter_doc, update_doc, return_document=None, sort=None):
        captured["filter"] = filter_doc
        return None  # claim nothing; we only assert on the filter shape

    store._channel_messages = SimpleNamespace(find_one_and_update=_fake_fou)  # type: ignore[attr-defined]

    claimed = await store.claim_pending_messages_for_extraction(
        batch_size=4,
        channel_id=None,
        settle_seconds=0,
        max_retries=5,
        purging_channel_ids={"P1", "P2"},
    )
    assert claimed == []
    chan_filter = captured["filter"]["channel_id"]  # type: ignore[index]
    assert isinstance(chan_filter, dict) and "$nin" in chan_filter
    assert set(chan_filter["$nin"]) == {"P1", "P2"}


async def test_extraction_claim_explicit_purging_channel_returns_empty() -> None:
    """Explicit single-channel claim for a purging channel claims nothing."""
    from beever_atlas.stores.mongodb_store import MongoDBStore

    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_messages = SimpleNamespace(  # type: ignore[attr-defined]
        find_one_and_update=AsyncMock(return_value=None)
    )
    claimed = await store.claim_pending_messages_for_extraction(
        batch_size=4,
        channel_id="P1",
        purging_channel_ids={"P1"},
    )
    assert claimed == []
    # Short-circuited before touching the collection.
    store._channel_messages.find_one_and_update.assert_not_awaited()


async def test_extraction_worker_tick_fetches_purging_set_once() -> None:
    """The worker fetches the purging set once per tick and threads it down."""
    from beever_atlas.services.extraction_worker import ExtractionWorker

    worker = ExtractionWorker()
    settings = SimpleNamespace(sync_batch_size=2, ingest_batch_concurrency=2)
    mongodb = SimpleNamespace(
        get_purging_channel_ids=AsyncMock(return_value={"P1"}),
        claim_pending_messages_for_extraction=AsyncMock(return_value=[]),
    )
    stores = SimpleNamespace(mongodb=mongodb)

    with (
        patch("beever_atlas.infra.config.get_settings", return_value=settings),
        patch("beever_atlas.stores.get_stores", return_value=stores),
    ):
        counters = await worker.tick()

    assert counters["claimed"] == 0
    mongodb.get_purging_channel_ids.assert_awaited_once()
    _, kwargs = mongodb.claim_pending_messages_for_extraction.await_args
    assert kwargs["purging_channel_ids"] == {"P1"}


# ─────────────────────────────────────────────────────────────────────────────
# Guard 5: SyncScheduler._execute_sync no-ops when purging
# ─────────────────────────────────────────────────────────────────────────────


async def test_execute_sync_noops_when_purging() -> None:
    from beever_atlas.services.scheduler import SyncScheduler

    sched = SyncScheduler.__new__(SyncScheduler)
    sched._global_semaphore = None
    stores = _stores_with_purging({"C1"}, is_purging=True)

    runner = SimpleNamespace(start_sync=AsyncMock())
    with (
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch(
            "beever_atlas.services.policy_resolver.resolve_effective_policy",
            new=AsyncMock(),
        ) as resolve_mock,
        patch("beever_atlas.api.sync.get_sync_runner", return_value=runner),
    ):
        await sched._execute_sync("C1")

    # Aborted before resolving policy or starting a sync.
    stores.mongodb.is_purging.assert_awaited_once_with("C1")
    resolve_mock.assert_not_awaited()
    runner.start_sync.assert_not_awaited()


# ─────────────────────────────────────────────────────────────────────────────
# Cancel helpers (process-local)
# ─────────────────────────────────────────────────────────────────────────────


async def test_cancel_sync_cancels_inflight_task() -> None:
    from beever_atlas.services.sync_runner import SyncRunner

    runner = SyncRunner()

    async def _never() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never())
    runner._active_tasks["C1"] = task
    # Yield so the task starts.
    await asyncio.sleep(0)

    cancelled = await runner.cancel_sync("C1")
    assert cancelled is True
    assert task.cancelled()
    assert "C1" not in runner._active_tasks


async def test_cancel_sync_returns_false_when_none() -> None:
    from beever_atlas.services.sync_runner import SyncRunner

    runner = SyncRunner()
    assert await runner.cancel_sync("nope") is False


async def test_cancel_consolidation_cancels_inflight_task() -> None:
    from beever_atlas.services import pipeline_orchestrator as orch

    orch._consolidation_tasks.clear()

    async def _never() -> None:
        await asyncio.sleep(3600)

    task = asyncio.create_task(_never())
    orch._consolidation_tasks["C1"] = task
    await asyncio.sleep(0)
    try:
        cancelled = await orch.cancel_consolidation("C1")
        assert cancelled is True
        assert task.cancelled()
        assert "C1" not in orch._consolidation_tasks
    finally:
        orch._consolidation_tasks.clear()


async def test_cancel_consolidation_returns_false_when_none() -> None:
    from beever_atlas.services import pipeline_orchestrator as orch

    orch._consolidation_tasks.clear()
    assert await orch.cancel_consolidation("nope") is False
