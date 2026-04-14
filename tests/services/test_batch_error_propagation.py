"""Tests that per-batch failures propagate to top-level BatchResult.errors.

Verifies that when _run_single_batch raises for some batches, those exceptions
are captured in result.errors and result.batch_breakdowns, while successful
batches still contribute their data.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.services.batch_processor import BatchProcessor


def _make_stores_mock() -> MagicMock:
    stores = MagicMock()
    stores.mongodb.update_sync_progress = AsyncMock(return_value=None)
    stores.mongodb.load_pipeline_checkpoint = AsyncMock(return_value=None)
    stores.mongodb.save_pipeline_checkpoint = AsyncMock(return_value=None)
    stores.mongodb.delete_pipeline_checkpoint = AsyncMock(return_value=None)
    stores.mongodb.increment_batches_completed = AsyncMock(return_value=None)
    stores.entity_registry.get_all_canonical = AsyncMock(return_value=[])
    return stores


def _make_settings_mock(concurrency: int = 4) -> MagicMock:
    settings = MagicMock()
    settings.sync_batch_size = 1
    settings.batch_max_prompt_tokens = 0
    settings.max_facts_per_message = 2
    settings.ingest_batch_concurrency = concurrency
    settings.language_detection_enabled = False
    return settings


def _make_runner_mock() -> MagicMock:
    async def _run_async(**kwargs):
        event = MagicMock()
        event.author = "persister"
        actions = MagicMock()
        actions.state_delta = {"persist_result": {"weaviate_ids": ["id1"], "entity_count": 1, "relationship_count": 0}}
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


@pytest.mark.asyncio
async def test_batch_exceptions_populate_result_errors() -> None:
    """Batches 2 and 4 crash; result.errors must have 2 entries, others succeed."""
    stores = _make_stores_mock()
    settings = _make_settings_mock(concurrency=4)
    runner = _make_runner_mock()
    session_svc = _make_session_service_mock()

    fake_session = MagicMock()
    fake_session.id = "sess-err"

    call_count = 0

    original_gather = asyncio.gather

    async def _patched_gather(*coros, return_exceptions=False):
        # Wrap each coroutine so batches 2 and 4 (index 1, 3) raise.
        async def _maybe_raise(idx, coro):
            if idx in (1, 3):
                # Drain the coroutine to avoid ResourceWarning then raise.
                try:
                    await coro
                except Exception:
                    pass
                raise RuntimeError(f"Simulated crash for batch {idx + 1}")
            return await coro

        wrapped = [_maybe_raise(i, c) for i, c in enumerate(coros)]
        return await original_gather(*wrapped, return_exceptions=return_exceptions)

    processor = BatchProcessor()

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch("beever_atlas.services.batch_processor.create_ingestion_pipeline", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch("beever_atlas.services.batch_processor.create_session", new=AsyncMock(return_value=fake_session)),
        patch("beever_atlas.agents.runner.get_session_service", return_value=session_svc),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor.asyncio.gather", side_effect=_patched_gather),
    ):
        messages = [{"text": f"msg{i}", "id": str(i)} for i in range(4)]
        result = await processor.process_messages(
            messages=messages,
            channel_id="C999",
            channel_name="err-test",
            sync_job_id="job-error-prop-test",
        )

    assert len(result.errors) == 2, f"Expected 2 errors, got {len(result.errors)}: {result.errors}"
    failed_batch_nums = {e["batch_num"] for e in result.errors}
    assert failed_batch_nums == {2, 4}, f"Expected batches 2 and 4 to fail, got {failed_batch_nums}"
    assert len(result.batch_breakdowns) == 4, "All 4 breakdowns must be present (2 successful + 2 failure placeholders)"


@pytest.mark.asyncio
async def test_all_batch_errors_empty_when_no_failures() -> None:
    """When no batch raises, result.errors must be empty."""
    stores = _make_stores_mock()
    settings = _make_settings_mock(concurrency=2)
    runner = _make_runner_mock()
    session_svc = _make_session_service_mock()

    fake_session = MagicMock()
    fake_session.id = "sess-ok"

    processor = BatchProcessor()

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch("beever_atlas.services.batch_processor.create_ingestion_pipeline", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch("beever_atlas.services.batch_processor.create_session", new=AsyncMock(return_value=fake_session)),
        patch("beever_atlas.agents.runner.get_session_service", return_value=session_svc),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
    ):
        messages = [{"text": f"msg{i}", "id": str(i)} for i in range(2)]
        result = await processor.process_messages(
            messages=messages,
            channel_id="C998",
            channel_name="ok-test",
            sync_job_id="job-no-errors-test",
        )

    assert len(result.errors) == 0
    assert len(result.batch_breakdowns) == 2
