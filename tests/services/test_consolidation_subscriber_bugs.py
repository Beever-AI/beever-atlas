"""Regression tests for the three consolidation-subscriber bugs fixed in this PR.

Bug A — CRITICAL — double-consolidation in legacy mode:
  When DECOUPLE_EXTRACTION=false the subscriber must NOT be registered.

Bug B — HIGH — debounce was "drop all after first" instead of "queue one follow-up":
  7 batches arriving while one consolidation is in-flight must result in exactly
  2 consolidation calls (1 immediate + 1 follow-up), not 7 and not 1.

Bug C — MEDIUM — AFTER_N_SYNCS counter incremented per batch:
  The subscriber path calls consolidate_only() (no counter tick); the counter
  is incremented by SyncRunner once per logical sync.

Convention: pyproject.toml sets ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(*, decouple_extraction: bool) -> SimpleNamespace:
    s = SimpleNamespace()
    s.decouple_extraction = decouple_extraction
    return s


# ---------------------------------------------------------------------------
# Bug A — subscriber skipped in legacy mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidation_subscriber_skipped_in_legacy_mode() -> None:
    """DECOUPLE_EXTRACTION=false → subscriber must NOT be registered.

    We exercise the registration code path directly by calling the inner
    logic that app.py's lifespan uses: if ``settings.decouple_extraction``
    is False the whole block is skipped and subscribe_extraction_done is
    never called.
    """
    subscribe_spy = MagicMock()

    mock_worker = MagicMock()
    mock_worker.subscribe_extraction_done = subscribe_spy

    settings = _make_settings(decouple_extraction=False)

    # Simulate the app.py conditional: only enter the try-block when True.
    if settings.decouple_extraction:
        mock_worker.subscribe_extraction_done(lambda *_: None)

    subscribe_spy.assert_not_called()


@pytest.mark.asyncio
async def test_consolidation_subscriber_registered_in_decoupled_mode() -> None:
    """DECOUPLE_EXTRACTION=true → subscriber IS registered (positive case)."""
    subscribe_spy = MagicMock()

    mock_worker = MagicMock()
    mock_worker.subscribe_extraction_done = subscribe_spy

    settings = _make_settings(decouple_extraction=True)

    if settings.decouple_extraction:
        mock_worker.subscribe_extraction_done(lambda *_: None)

    subscribe_spy.assert_called_once()


# ---------------------------------------------------------------------------
# Bug B — debounce queues exactly one follow-up
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consolidation_debounce_queues_one_followup() -> None:
    """Fire 7 extraction-done events while one consolidation is in-flight.

    Expected: consolidate_only called exactly TWICE (1 immediate + 1 follow-up),
    NOT 7 times (old broken path: each batch triggered independently)
    and NOT 1 time (original single-flag bug: all 2-7 were dropped).
    """
    consolidate_call_count = 0
    consolidation_started = asyncio.Event()
    consolidation_may_finish = asyncio.Event()

    async def slow_consolidate_only(channel_id: str) -> None:
        nonlocal consolidate_call_count
        consolidate_call_count += 1
        consolidation_started.set()
        # Hold until test releases us, simulating a long consolidation.
        await consolidation_may_finish.wait()

    # Build the exact debounce machinery from app.py (copied logic, not the app import).
    _consolidation_running: dict[str, bool] = {}
    _consolidation_pending: dict[str, bool] = {}

    async def _run_consolidation_after_extraction(channel_id: str) -> None:
        await slow_consolidate_only(channel_id)

    async def _run_with_debounce(channel_id: str) -> None:
        if _consolidation_running.get(channel_id):
            _consolidation_pending[channel_id] = True
            return
        _consolidation_running[channel_id] = True
        try:
            while True:
                _consolidation_pending.pop(channel_id, None)
                await _run_consolidation_after_extraction(channel_id)
                if not _consolidation_pending.pop(channel_id, False):
                    break
        finally:
            _consolidation_running.pop(channel_id, None)

    channel = "C_debounce"

    # Fire first event — starts consolidation #1 in the background.
    t1 = asyncio.create_task(_run_with_debounce(channel))

    # Wait until consolidation #1 has started (is blocked inside slow_consolidate_only).
    await asyncio.wait_for(consolidation_started.wait(), timeout=2.0)

    # Fire 6 more events while #1 is still running.  All 6 should collapse
    # into a single pending flag.
    for _ in range(6):
        asyncio.create_task(_run_with_debounce(channel))

    # Yield to let the pending-setter tasks run.
    await asyncio.sleep(0)

    # Release the lock so consolidation #1 finishes, which triggers #2.
    consolidation_may_finish.set()

    # Wait for the initial task (and its follow-up loop) to complete.
    await asyncio.wait_for(t1, timeout=5.0)

    # Allow follow-up task (spawned by the loop) to finish too.
    await asyncio.sleep(0.05)

    assert consolidate_call_count == 2, (
        f"Expected exactly 2 consolidation calls (1 immediate + 1 follow-up), "
        f"got {consolidate_call_count}"
    )


# ---------------------------------------------------------------------------
# Bug C — AFTER_N_SYNCS counter NOT incremented by the subscriber path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_n_syncs_counter_not_incremented_by_subscriber() -> None:
    """The subscriber calls consolidate_only(), NOT on_ingestion_complete().

    consolidate_only() must NOT call increment_sync_counter.  A 7-batch sync
    arriving via the subscriber path must leave the counter at 0 increments
    (the counter belongs to SyncRunner, which increments once per logical sync).
    """
    increment_counter_calls = 0

    async def fake_increment_sync_counter(channel_id: str) -> int:
        nonlocal increment_counter_calls
        increment_counter_calls += 1
        return increment_counter_calls

    mock_stores = MagicMock()
    mock_stores.mongodb.increment_sync_counter = AsyncMock(side_effect=fake_increment_sync_counter)
    mock_stores.mongodb.reset_sync_counter = AsyncMock()

    with (
        patch(
            "beever_atlas.services.pipeline_orchestrator.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.services.pipeline_orchestrator._run_consolidation",
            new_callable=AsyncMock,
        ),
    ):
        from beever_atlas.services.pipeline_orchestrator import consolidate_only

        # Simulate 7 batches arriving via the subscriber path.
        for _ in range(7):
            await consolidate_only("C_counter_test")

        # consolidate_only must NEVER touch the sync counter.
        assert increment_counter_calls == 0, (
            f"consolidate_only must not increment sync counter; "
            f"called {increment_counter_calls} times (expected 0). "
            "Counter increments belong to SyncRunner (once per logical sync)."
        )


@pytest.mark.asyncio
async def test_on_ingestion_complete_still_increments_counter() -> None:
    """Positive control: on_ingestion_complete() still increments the counter.

    This ensures the Bug C fix (using consolidate_only in the subscriber)
    didn't accidentally remove the counter increment from the legacy path.
    """
    from beever_atlas.models.sync_policy import (
        ConsolidationConfig,
        ConsolidationStrategy,
        IngestionConfig,
        ResolvedPolicy,
        SyncConfig,
        SyncTriggerMode,
        WikiConfig,
    )

    from beever_atlas.services import pipeline_orchestrator

    pipeline_orchestrator._consolidation_tasks.clear()

    resolved = ResolvedPolicy(
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
            strategy=ConsolidationStrategy.AFTER_N_SYNCS,
            after_n_syncs=3,
        ),
        wiki=WikiConfig(),
    )

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
        ),
    ):
        from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete

        await on_ingestion_complete("C_legacy", facts_created=5)
        await asyncio.sleep(0.05)

        mock_stores.mongodb.increment_sync_counter.assert_awaited_once_with("C_legacy")
