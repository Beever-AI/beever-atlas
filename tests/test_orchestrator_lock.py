"""Race-safety tests for the pipeline orchestrator per-channel lock."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from beever_atlas.models.sync_policy import (
    ConsolidationConfig,
    ConsolidationStrategy,
    IngestionConfig,
    ResolvedPolicy,
    SyncConfig,
    SyncTriggerMode,
    WikiConfig,
)


def _make_resolved() -> ResolvedPolicy:
    return ResolvedPolicy(
        sync=SyncConfig(
            trigger_mode=SyncTriggerMode.MANUAL,
            sync_type="auto",
            max_messages=1000,
            min_sync_interval_minutes=5,
        ),
        ingestion=IngestionConfig(
            batch_size=10,
            quality_threshold=0.5,
            max_facts_per_message=2,
            skip_entity_extraction=False,
            skip_graph_writes=False,
        ),
        consolidation=ConsolidationConfig(
            strategy=ConsolidationStrategy.AFTER_EVERY_SYNC,
            after_n_syncs=3,
            similarity_threshold=0.6,
            merge_threshold=0.85,
            min_facts_for_clustering=3,
            staleness_refresh_days=7,
        ),
        wiki=WikiConfig(),
    )


@pytest.fixture(autouse=True)
def _clear_state():
    from beever_atlas.services import pipeline_orchestrator
    pipeline_orchestrator._consolidation_tasks.clear()
    yield
    pipeline_orchestrator._consolidation_tasks.clear()


@pytest.mark.asyncio
async def test_concurrent_spawn_only_creates_one_task():
    """N back-to-back ``_spawn_consolidation`` calls for the same channel
    must yield exactly one running task. Dedup is synchronous dict
    membership with no ``await`` between check and insert."""
    from beever_atlas.services import pipeline_orchestrator

    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0

    async def _slow_consolidation(channel_id: str) -> None:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()

    with patch.object(
        pipeline_orchestrator, "_run_consolidation", side_effect=_slow_consolidation
    ):
        for _ in range(25):
            pipeline_orchestrator._spawn_consolidation("C123")
        await started.wait()
        # Exactly one task should have been created.
        assert len([t for t in pipeline_orchestrator._consolidation_tasks.values()
                    if not t.done()]) == 1
        release.set()
        # Let the background task finish cleanly.
        task = pipeline_orchestrator._consolidation_tasks.get("C123")
        if task is not None:
            await task

    assert invocations == 1


@pytest.mark.asyncio
async def test_concurrent_on_ingestion_only_spawns_one_task():
    """Two concurrent ``on_ingestion_complete`` calls for the same channel
    must not race past the lock and double-spawn."""
    from beever_atlas.services import pipeline_orchestrator

    started = asyncio.Event()
    release = asyncio.Event()
    invocations = 0

    async def _slow_consolidation(channel_id: str) -> None:
        nonlocal invocations
        invocations += 1
        started.set()
        await release.wait()

    with (
        patch.object(
            pipeline_orchestrator,
            "resolve_effective_policy",
            new_callable=AsyncMock,
            return_value=_make_resolved(),
        ),
        patch.object(
            pipeline_orchestrator,
            "_run_consolidation",
            side_effect=_slow_consolidation,
        ),
    ):
        await asyncio.gather(
            pipeline_orchestrator.on_ingestion_complete("C123", facts_created=1),
            pipeline_orchestrator.on_ingestion_complete("C123", facts_created=2),
            pipeline_orchestrator.on_ingestion_complete("C123", facts_created=3),
        )
        await started.wait()
        release.set()
        task = pipeline_orchestrator._consolidation_tasks.get("C123")
        if task is not None:
            await task

    assert invocations == 1


@pytest.mark.asyncio
async def test_different_channels_spawn_independent_tasks():
    """Locks are keyed by channel_id, so distinct channels must not
    serialize against each other."""
    from beever_atlas.services import pipeline_orchestrator

    release = asyncio.Event()
    invocations: list[str] = []

    async def _slow_consolidation(channel_id: str) -> None:
        invocations.append(channel_id)
        await release.wait()

    with patch.object(
        pipeline_orchestrator, "_run_consolidation", side_effect=_slow_consolidation
    ):
        pipeline_orchestrator._spawn_consolidation("C1")
        pipeline_orchestrator._spawn_consolidation("C2")
        pipeline_orchestrator._spawn_consolidation("C3")
        # Yield so the created tasks can begin executing.
        await asyncio.sleep(0)
        release.set()
        for cid in ("C1", "C2", "C3"):
            task = pipeline_orchestrator._consolidation_tasks.get(cid)
            if task is not None:
                await task

    assert sorted(invocations) == ["C1", "C2", "C3"]
