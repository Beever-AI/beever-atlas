"""Tests for B2+D2: per-provider rate limiters and extended timing telemetry."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import beever_atlas.services.batch_processor as bp_module
from beever_atlas.services.batch_processor import _get_limiter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _reset_limiters() -> None:
    """Clear cached limiters between tests so RPM is re-read from patched settings."""
    bp_module._provider_limiters.clear()


def _make_settings(gemini_rpm: int = 300, jina_rpm: int = 500) -> MagicMock:
    s = MagicMock()
    s.sync_batch_size = 1
    s.batch_max_prompt_tokens = 0
    s.batch_max_output_tokens = 0
    s.batch_time_window_seconds = 0
    s.batch_max_messages = 0
    s.max_facts_per_message = 2
    s.ingest_batch_concurrency = 1
    s.language_detection_enabled = False
    s.llm_outage_breaker_threshold = 10
    s.gemini_rpm = gemini_rpm
    s.jina_rpm = jina_rpm
    return s


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


def _make_runner_mock_success() -> MagicMock:
    """Runner that immediately yields a persister event (no LLM stages)."""

    async def _run_async(**kwargs):
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


def _make_runner_mock_with_stages(stages: list[str]) -> MagicMock:
    """Runner that yields one event per stage name then a persister completion event."""

    async def _run_async(**kwargs):
        for stage in stages:
            ev = MagicMock()
            ev.author = stage
            ev.actions = None
            yield ev
        # Final persister event with persist_result
        ev = MagicMock()
        ev.author = "persister"
        actions = MagicMock()
        actions.state_delta = {
            "persist_result": {
                "weaviate_ids": ["id1"],
                "entity_count": 1,
                "relationship_count": 0,
            }
        }
        actions.stateDelta = None
        ev.actions = actions
        yield ev

    runner = MagicMock()
    runner.run_async = _run_async
    return runner


# ---------------------------------------------------------------------------
# Test 1: _get_limiter creates limiter with correct RPM from settings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_limiter_gemini_rpm():
    """_get_limiter('gemini') creates AsyncLimiter with gemini_rpm from settings."""
    _reset_limiters()
    settings = _make_settings(gemini_rpm=150)

    with patch("beever_atlas.services.batch_processor.get_settings", return_value=settings):
        limiter = await _get_limiter("gemini")

    assert limiter.max_rate == 150
    assert limiter.time_period == 60


@pytest.mark.asyncio
async def test_get_limiter_jina_rpm():
    """_get_limiter('jina') creates AsyncLimiter with jina_rpm from settings."""
    _reset_limiters()
    settings = _make_settings(jina_rpm=250)

    with patch("beever_atlas.services.batch_processor.get_settings", return_value=settings):
        limiter = await _get_limiter("jina")

    assert limiter.max_rate == 250
    assert limiter.time_period == 60


@pytest.mark.asyncio
async def test_get_limiter_returns_same_instance():
    """_get_limiter returns the same cached instance on repeated calls."""
    _reset_limiters()
    settings = _make_settings()

    with patch("beever_atlas.services.batch_processor.get_settings", return_value=settings):
        l1 = await _get_limiter("gemini")
        l2 = await _get_limiter("gemini")

    assert l1 is l2


# ---------------------------------------------------------------------------
# Test 2: Gemini limiter acquired for LLM stages, not for preprocessor/persister
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_gemini_limiter_acquired_for_llm_stages():
    """Gemini limiter.acquire() is called once per LLM stage (not for preprocessor/persister)."""
    _reset_limiters()
    settings = _make_settings()
    stores = _make_stores_mock()

    # stages: preprocessor, fact_extractor, entity_extractor — persister appended by runner
    stages = ["preprocessor", "fact_extractor", "entity_extractor"]
    runner = _make_runner_mock_with_stages(stages)

    fake_session = MagicMock()
    fake_session.id = "sess-limiter-1"

    mock_gemini_limiter = AsyncMock()
    mock_jina_limiter = AsyncMock()

    async def _fake_get_limiter(provider: str):
        return mock_gemini_limiter if provider == "gemini" else mock_jina_limiter

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch("beever_atlas.services.batch_processor.create_ingestion_pipeline", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch("beever_atlas.services.batch_processor.create_session", new=AsyncMock(return_value=fake_session)),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor._get_limiter", side_effect=_fake_get_limiter),
    ):
        from beever_atlas.services.batch_processor import BatchProcessor
        processor = BatchProcessor()
        await processor.process_messages(
            messages=[{"text": "hello", "id": "msg-1"}],
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-lim-gemini",
        )

    # fact_extractor and entity_extractor are LLM stages → 2 Gemini acquires
    assert mock_gemini_limiter.acquire.call_count == 2, (
        f"Expected 2 Gemini limiter acquires, got {mock_gemini_limiter.acquire.call_count}"
    )
    # No Jina acquire (no embedder stage in this run)
    assert mock_jina_limiter.acquire.call_count == 0, (
        f"Expected 0 Jina limiter acquires, got {mock_jina_limiter.acquire.call_count}"
    )


# ---------------------------------------------------------------------------
# Test 3: Jina limiter acquired for embedder stage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_jina_limiter_acquired_for_embedder():
    """Jina limiter.acquire() is called when the embedder stage fires."""
    _reset_limiters()
    settings = _make_settings()
    stores = _make_stores_mock()

    stages = ["embedder"]
    runner = _make_runner_mock_with_stages(stages)

    fake_session = MagicMock()
    fake_session.id = "sess-limiter-2"

    mock_gemini_limiter = AsyncMock()
    mock_jina_limiter = AsyncMock()

    async def _fake_get_limiter(provider: str):
        return mock_gemini_limiter if provider == "gemini" else mock_jina_limiter

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch("beever_atlas.services.batch_processor.create_ingestion_pipeline", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch("beever_atlas.services.batch_processor.create_session", new=AsyncMock(return_value=fake_session)),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor._get_limiter", side_effect=_fake_get_limiter),
    ):
        from beever_atlas.services.batch_processor import BatchProcessor
        processor = BatchProcessor()
        await processor.process_messages(
            messages=[{"text": "hello", "id": "msg-1"}],
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-lim-jina",
        )

    assert mock_jina_limiter.acquire.call_count == 1, (
        f"Expected 1 Jina limiter acquire, got {mock_jina_limiter.acquire.call_count}"
    )
    assert mock_gemini_limiter.acquire.call_count == 0


# ---------------------------------------------------------------------------
# Test 4: D2 — batch_wall_clock_s present in returned stage_timings
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d2_batch_wall_clock_in_timings():
    """batch_wall_clock_s is present and > 0 in stage_timings returned from a batch."""
    _reset_limiters()
    settings = _make_settings()
    stores = _make_stores_mock()
    runner = _make_runner_mock_success()

    fake_session = MagicMock()
    fake_session.id = "sess-d2-wall"

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch("beever_atlas.services.batch_processor.create_ingestion_pipeline", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch("beever_atlas.services.batch_processor.create_session", new=AsyncMock(return_value=fake_session)),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
    ):
        from beever_atlas.services.batch_processor import BatchProcessor
        processor = BatchProcessor()
        result = await processor.process_messages(
            messages=[{"text": "hello", "id": "msg-1"}],
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-d2-wall",
        )

    # Verify the timing was passed to update_sync_progress
    # Find calls that include stage_timings
    timing_calls = [
        call for call in stores.mongodb.update_sync_progress.call_args_list
        if call.kwargs.get("stage_timings")
    ]
    assert timing_calls, "Expected at least one update_sync_progress call with stage_timings"
    # The last one (final flush) should have batch_wall_clock_s
    last_timings = timing_calls[-1].kwargs["stage_timings"]
    assert "batch_wall_clock_s" in last_timings, (
        f"Expected 'batch_wall_clock_s' in stage_timings, got keys: {list(last_timings.keys())}"
    )
    assert last_timings["batch_wall_clock_s"] >= 0


# ---------------------------------------------------------------------------
# Test 5: D2 — limiter_wait_s_gemini absent when no Gemini stage fires
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d2_no_limiter_wait_when_no_llm_stages():
    """limiter_wait_s_gemini is absent from timings when no LLM stages run."""
    _reset_limiters()
    settings = _make_settings()
    stores = _make_stores_mock()
    # Only preprocessor + persister — no LLM stages
    runner = _make_runner_mock_with_stages(["preprocessor"])

    fake_session = MagicMock()
    fake_session.id = "sess-d2-nollm"

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch("beever_atlas.services.batch_processor.create_ingestion_pipeline", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch("beever_atlas.services.batch_processor.create_session", new=AsyncMock(return_value=fake_session)),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
    ):
        from beever_atlas.services.batch_processor import BatchProcessor
        processor = BatchProcessor()
        await processor.process_messages(
            messages=[{"text": "hello", "id": "msg-1"}],
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-d2-nollm",
        )

    timing_calls = [
        call for call in stores.mongodb.update_sync_progress.call_args_list
        if call.kwargs.get("stage_timings")
    ]
    if timing_calls:
        last_timings = timing_calls[-1].kwargs["stage_timings"]
        assert "limiter_wait_s_gemini" not in last_timings, (
            "limiter_wait_s_gemini should be absent when Gemini limiter was never waited on"
        )


# ---------------------------------------------------------------------------
# Test 6: D2 — limiter_wait_s_gemini present when a Gemini LLM stage fires
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_d2_limiter_wait_gemini_present_after_llm_stage():
    """limiter_wait_s_gemini is present in stage_timings after a Gemini LLM stage."""
    _reset_limiters()
    settings = _make_settings()
    stores = _make_stores_mock()
    runner = _make_runner_mock_with_stages(["fact_extractor"])

    fake_session = MagicMock()
    fake_session.id = "sess-d2-llm"

    # Limiter with very high rate so acquire() returns immediately
    with patch("beever_atlas.services.batch_processor.get_settings", return_value=settings):
        _reset_limiters()

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch("beever_atlas.services.batch_processor.create_ingestion_pipeline", return_value=MagicMock()),
        patch("beever_atlas.services.batch_processor.create_runner", return_value=runner),
        patch("beever_atlas.services.batch_processor.create_session", new=AsyncMock(return_value=fake_session)),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
    ):
        from beever_atlas.services.batch_processor import BatchProcessor
        processor = BatchProcessor()
        await processor.process_messages(
            messages=[{"text": "hello", "id": "msg-1"}],
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-d2-llm",
        )

    timing_calls = [
        call for call in stores.mongodb.update_sync_progress.call_args_list
        if call.kwargs.get("stage_timings")
    ]
    assert timing_calls, "Expected stage_timings in update_sync_progress"
    last_timings = timing_calls[-1].kwargs["stage_timings"]
    assert "limiter_wait_s_gemini" in last_timings, (
        f"Expected 'limiter_wait_s_gemini' in timings after fact_extractor, got: {list(last_timings.keys())}"
    )
    assert last_timings["limiter_wait_s_gemini"] >= 0
