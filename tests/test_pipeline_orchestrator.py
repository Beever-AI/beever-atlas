"""Unit tests for pipeline orchestrator consolidation strategies."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.models.sync_policy import (
    ConsolidationConfig,
    ConsolidationStrategy,
    GlobalPolicyDefaults,
    IngestionConfig,
    ResolvedPolicy,
    SyncConfig,
    SyncTriggerMode,
    WikiConfig,
)


def _make_resolved(strategy: ConsolidationStrategy, after_n: int = 3) -> ResolvedPolicy:
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
            strategy=strategy,
            after_n_syncs=after_n,
            similarity_threshold=0.6,
            merge_threshold=0.85,
            min_facts_for_clustering=3,
            staleness_refresh_days=7,
        ),
        wiki=WikiConfig(),
    )


@pytest.fixture(autouse=True)
def _clear_tasks():
    """Clear consolidation task tracking between tests."""
    from beever_atlas.services import pipeline_orchestrator
    pipeline_orchestrator._consolidation_tasks.clear()
    yield
    pipeline_orchestrator._consolidation_tasks.clear()


@pytest.mark.asyncio
async def test_after_every_sync_triggers_consolidation():
    """after_every_sync strategy should spawn consolidation immediately."""
    resolved = _make_resolved(ConsolidationStrategy.AFTER_EVERY_SYNC)

    with (
        patch(
            "beever_atlas.services.pipeline_orchestrator.resolve_effective_policy",
            new_callable=AsyncMock,
            return_value=resolved,
        ),
        patch(
            "beever_atlas.services.pipeline_orchestrator._run_consolidation",
            new_callable=AsyncMock,
        ) as mock_consolidation,
    ):
        from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete

        await on_ingestion_complete("C123", facts_created=10)

        # Give the spawned task a moment to start
        await asyncio.sleep(0.1)
        mock_consolidation.assert_called_once_with("C123")


@pytest.mark.asyncio
async def test_after_n_syncs_triggers_at_threshold():
    """after_n_syncs strategy should trigger when counter reaches threshold."""
    resolved = _make_resolved(ConsolidationStrategy.AFTER_N_SYNCS, after_n=3)

    mock_stores = MagicMock()
    mock_stores.mongodb.increment_sync_counter = AsyncMock(return_value=3)
    mock_stores.mongodb.reset_sync_counter = AsyncMock()

    with (
        patch(
            "beever_atlas.services.pipeline_orchestrator.resolve_effective_policy",
            new_callable=AsyncMock,
            return_value=resolved,
        ),
        patch(
            "beever_atlas.services.pipeline_orchestrator.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.services.pipeline_orchestrator._run_consolidation",
            new_callable=AsyncMock,
        ) as mock_consolidation,
    ):
        from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete

        await on_ingestion_complete("C123", facts_created=5)

        await asyncio.sleep(0.1)
        mock_consolidation.assert_called_once_with("C123")
        # Counter reset happens inside _run_consolidation (not on_ingestion_complete)


@pytest.mark.asyncio
async def test_after_n_syncs_does_not_trigger_below_threshold():
    """after_n_syncs strategy should NOT trigger when counter is below threshold."""
    resolved = _make_resolved(ConsolidationStrategy.AFTER_N_SYNCS, after_n=3)

    mock_stores = MagicMock()
    mock_stores.mongodb.increment_sync_counter = AsyncMock(return_value=1)

    with (
        patch(
            "beever_atlas.services.pipeline_orchestrator.resolve_effective_policy",
            new_callable=AsyncMock,
            return_value=resolved,
        ),
        patch(
            "beever_atlas.services.pipeline_orchestrator.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.services.pipeline_orchestrator._run_consolidation",
            new_callable=AsyncMock,
        ) as mock_consolidation,
    ):
        from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete

        await on_ingestion_complete("C123", facts_created=5)

        await asyncio.sleep(0.1)
        mock_consolidation.assert_not_called()


@pytest.mark.asyncio
async def test_scheduled_strategy_skips_consolidation():
    """scheduled strategy should NOT trigger consolidation after ingestion."""
    resolved = _make_resolved(ConsolidationStrategy.SCHEDULED)

    with (
        patch(
            "beever_atlas.services.pipeline_orchestrator.resolve_effective_policy",
            new_callable=AsyncMock,
            return_value=resolved,
        ),
        patch(
            "beever_atlas.services.pipeline_orchestrator._run_consolidation",
            new_callable=AsyncMock,
        ) as mock_consolidation,
    ):
        from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete

        await on_ingestion_complete("C123", facts_created=5)

        await asyncio.sleep(0.1)
        mock_consolidation.assert_not_called()


@pytest.mark.asyncio
async def test_manual_strategy_skips_consolidation():
    """manual strategy should NOT trigger consolidation after ingestion."""
    resolved = _make_resolved(ConsolidationStrategy.MANUAL)

    with (
        patch(
            "beever_atlas.services.pipeline_orchestrator.resolve_effective_policy",
            new_callable=AsyncMock,
            return_value=resolved,
        ),
        patch(
            "beever_atlas.services.pipeline_orchestrator._run_consolidation",
            new_callable=AsyncMock,
        ) as mock_consolidation,
    ):
        from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete

        await on_ingestion_complete("C123", facts_created=5)

        await asyncio.sleep(0.1)
        mock_consolidation.assert_not_called()
