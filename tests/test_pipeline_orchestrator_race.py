"""Regression: concurrent _spawn_consolidation for the same channel must dedupe.

Prevents a race where N concurrent ``on_ingestion_complete`` calls for the
same channel spawn N consolidation tasks (duplicate work, Mongo contention).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from beever_atlas.services import pipeline_orchestrator


@pytest.fixture(autouse=True)
def _clear_tasks():
    pipeline_orchestrator._consolidation_tasks.clear()
    yield
    pipeline_orchestrator._consolidation_tasks.clear()


@pytest.mark.asyncio
async def test_concurrent_spawn_produces_single_task():
    """20 concurrent _spawn_consolidation calls → exactly one entry in the tracking dict."""

    async def _slow_run(_channel_id: str) -> None:
        # Hold the task alive long enough that all spawners see it as running.
        await asyncio.sleep(0.1)

    with patch(
        "beever_atlas.services.pipeline_orchestrator._run_consolidation",
        side_effect=_slow_run,
    ):
        # Fire many spawns within the same loop tick — the dedup check is
        # synchronous (dict membership), so back-to-back calls on the same
        # loop exercise the race guard.
        for _ in range(20):
            pipeline_orchestrator._spawn_consolidation("C1")

        assert "C1" in pipeline_orchestrator._consolidation_tasks
        assert len(pipeline_orchestrator._consolidation_tasks) == 1

        # Let the single task finish so the fixture cleanup is clean.
        await pipeline_orchestrator._consolidation_tasks["C1"]

        # After completion, a fresh spawn should create a new task (guard only
        # dedupes while the previous task is still running).
        pipeline_orchestrator._spawn_consolidation("C1")
        assert len(pipeline_orchestrator._consolidation_tasks) == 1
        await pipeline_orchestrator._consolidation_tasks["C1"]
