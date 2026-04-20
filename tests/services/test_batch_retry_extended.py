"""Extended retry-ladder tests for BatchProcessor.

Verifies that:
1. asyncio.sleep is called with the base backoff values (no jitter when
   random.uniform returns 0.0).
2. The last sleep call uses base * 1.25 when random.uniform returns 0.25.

Step 3 — ingestion-batch-cap-retry-v2 plan.
"""

from __future__ import annotations

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


def _make_settings_mock() -> MagicMock:
    settings = MagicMock()
    settings.sync_batch_size = 1
    settings.batch_max_prompt_tokens = 0
    settings.batch_max_output_tokens = 0
    settings.batch_time_window_seconds = 0
    settings.batch_max_messages = 0
    settings.max_facts_per_message = 2
    settings.ingest_batch_concurrency = 1
    settings.language_detection_enabled = False
    settings.llm_outage_breaker_threshold = 10
    return settings


def _make_runner_mock_failing(fail_times: int) -> MagicMock:
    """Runner whose run_async raises ServerError for the first *fail_times* calls then yields a success event."""
    from google.genai.errors import ServerError

    call_count = 0

    async def _run_async(**kwargs):
        nonlocal call_count
        call_count += 1
        if call_count <= fail_times:
            raise ServerError(503, {"error": "transient"})

        # Success path: yield a persister completion event
        event = MagicMock()
        event.author = "persister"
        actions = MagicMock()
        actions.state_delta = {
            "persist_result": {
                "weaviate_ids": ["id1"],
                "entity_count": 1,
                "relationship_count": 0,
            }
        }
        actions.stateDelta = None
        event.actions = actions
        yield event

    runner = MagicMock()
    runner.run_async = _run_async
    return runner


# ---------------------------------------------------------------------------
# Test 1 — base backoff values (jitter = 0)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_sleep_calls_base_values():
    """With random.uniform patched to 0.0, sleep args equal the base ladder."""
    stores = _make_stores_mock()
    settings = _make_settings_mock()

    # Runner fails 4 times then succeeds → 4 sleeps with base values
    runner = _make_runner_mock_failing(fail_times=4)

    fake_session = MagicMock()
    fake_session.id = "sess-1"

    processor = BatchProcessor()

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    with (
        patch("beever_atlas.services.batch_processor.random.uniform", return_value=0.0),
        patch("beever_atlas.services.batch_processor.asyncio.sleep", side_effect=_fake_sleep),
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
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
    ):
        await processor.process_messages(
            messages=[{"text": "hello", "id": "msg-1"}],
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-retry-base",
        )

    # 4 failed attempts → 4 retry sleeps with base values [30, 60, 120, 240]
    assert sleep_calls == [30.0, 60.0, 120.0, 240.0], f"Unexpected sleep calls: {sleep_calls}"


# ---------------------------------------------------------------------------
# Test 2 — jitter applied (uniform returns 0.25)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_sleep_last_call_with_jitter():
    """With random.uniform patched to 0.25, last sleep = 480 * 1.25."""
    stores = _make_stores_mock()
    settings = _make_settings_mock()

    # Runner fails 5 times then succeeds → 5 sleeps
    runner = _make_runner_mock_failing(fail_times=5)

    fake_session = MagicMock()
    fake_session.id = "sess-2"

    processor = BatchProcessor()

    sleep_calls: list[float] = []

    async def _fake_sleep(secs: float) -> None:
        sleep_calls.append(secs)

    with (
        patch("beever_atlas.services.batch_processor.random.uniform", return_value=0.25),
        patch("beever_atlas.services.batch_processor.asyncio.sleep", side_effect=_fake_sleep),
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
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
    ):
        await processor.process_messages(
            messages=[{"text": "hello", "id": "msg-1"}],
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-retry-jitter",
        )

    assert len(sleep_calls) == 5, f"Expected 5 sleep calls, got {len(sleep_calls)}"
    expected_last = 480 * 1.25
    assert abs(sleep_calls[-1] - expected_last) < 0.01, (
        f"Last sleep expected ~{expected_last}, got {sleep_calls[-1]}"
    )
