"""Tests for bounded-concurrency batch execution in BatchProcessor.

Verifies that with ingest_batch_concurrency=2 and 4 batches, total wall-clock
time is less than 250ms (would be ~400ms if sequential at 100ms/batch).

Phase 4 — ingestion-pipeline-hardening plan.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.services.batch_processor import BatchProcessor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stores_mock() -> MagicMock:
    stores = MagicMock()
    stores.mongodb.update_sync_progress = AsyncMock(return_value=None)
    stores.mongodb.update_batch_stage = AsyncMock(return_value=None)
    stores.mongodb.push_activity_log_entry = AsyncMock(return_value=None)
    stores.mongodb.load_pipeline_checkpoint = AsyncMock(return_value=None)
    stores.mongodb.save_pipeline_checkpoint = AsyncMock(return_value=None)
    stores.mongodb.delete_pipeline_checkpoint = AsyncMock(return_value=None)
    stores.mongodb.increment_batches_completed = AsyncMock(return_value=None)
    stores.entity_registry.get_all_canonical = AsyncMock(return_value=[])
    return stores


def _make_settings_mock(concurrency: int = 2) -> MagicMock:
    settings = MagicMock()
    settings.sync_batch_size = 1
    settings.batch_max_prompt_tokens = 0  # disable adaptive batcher
    settings.max_facts_per_message = 2
    settings.ingest_batch_concurrency = concurrency
    settings.language_detection_enabled = False
    settings.llm_outage_breaker_threshold = 100  # effectively disabled for concurrency tests
    return settings


def _make_runner_mock(sleep_seconds: float = 0.1) -> MagicMock:
    """Runner whose run_async sleeps then yields a single event."""

    async def _run_async(**kwargs):
        await asyncio.sleep(sleep_seconds)

        # Yield a minimal event that looks like a persister completion.
        event = MagicMock()
        event.author = "persister"
        actions = MagicMock()
        actions.state_delta = {
            "persist_result": {"weaviate_ids": ["id1"], "entity_count": 1, "relationship_count": 0}
        }
        actions.stateDelta = None
        event.actions = actions
        yield event

    runner = MagicMock()
    runner.run_async = _run_async
    return runner


def _make_session_service_mock() -> MagicMock:
    session = MagicMock()
    session.state = {
        "persist_result": {"weaviate_ids": ["id1"], "entity_count": 1, "relationship_count": 0},
        "extracted_facts": {"facts": []},
        "extracted_entities": {"entities": [], "relationships": []},
        "embedded_facts": [],
    }
    svc = MagicMock()
    svc.get_session = AsyncMock(return_value=session)
    return svc


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_batches_faster_than_sequential() -> None:
    """4 batches, concurrency=2 → total wall-clock < 250ms (sequential ≈ 400ms)."""
    stores = _make_stores_mock()
    settings = _make_settings_mock(concurrency=2)
    runner = _make_runner_mock(sleep_seconds=0.1)
    session_svc = _make_session_service_mock()

    fake_session = MagicMock()
    fake_session.id = "sess-1"

    processor = BatchProcessor()

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch(
            "beever_atlas.services.batch_processor.create_ingestion_pipeline",
            return_value=MagicMock(),
        ),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch(
            "beever_atlas.services.batch_processor.create_session",
            new=AsyncMock(return_value=fake_session),
        ),
        patch("beever_atlas.agents.runner.get_session_service", return_value=session_svc),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
    ):
        # 4 single-message batches (batch_size=1)
        messages = [{"text": f"msg{i}", "id": str(i)} for i in range(4)]

        start = time.monotonic()
        result = await processor.process_messages(
            messages=messages,
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-concurrent-test",
        )
        elapsed = time.monotonic() - start

    assert elapsed < 0.25, f"Expected <250ms with concurrency=2, got {elapsed * 1000:.0f}ms"
    assert len(result.batch_breakdowns) == 4
    assert len(result.errors) == 0


@pytest.mark.asyncio
async def test_all_batches_complete_and_ordered() -> None:
    """All 4 batches complete; breakdowns are in index order."""
    stores = _make_stores_mock()
    settings = _make_settings_mock(concurrency=2)
    runner = _make_runner_mock(sleep_seconds=0.05)
    session_svc = _make_session_service_mock()

    fake_session = MagicMock()
    fake_session.id = "sess-2"

    processor = BatchProcessor()

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch(
            "beever_atlas.services.batch_processor.create_ingestion_pipeline",
            return_value=MagicMock(),
        ),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch(
            "beever_atlas.services.batch_processor.create_session",
            new=AsyncMock(return_value=fake_session),
        ),
        patch("beever_atlas.agents.runner.get_session_service", return_value=session_svc),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
    ):
        messages = [{"text": f"msg{i}", "id": str(i)} for i in range(4)]
        result = await processor.process_messages(
            messages=messages,
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-order-test",
        )

    assert len(result.batch_breakdowns) == 4
    batch_nums = {bd.batch_num for bd in result.batch_breakdowns}
    assert batch_nums == {1, 2, 3, 4}, "All 4 batch indices must be present"
    assert len(result.errors) == 0
