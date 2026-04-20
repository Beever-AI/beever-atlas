"""Tests that per-batch failures propagate to top-level BatchResult.errors.

Verifies that when _run_single_batch raises for some batches, those exceptions
are captured in result.errors and result.batch_breakdowns, while successful
batches still contribute their data.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.services.batch_processor import BatchProcessor


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


def _make_settings_mock(concurrency: int = 4) -> MagicMock:
    settings = MagicMock()
    settings.sync_batch_size = 1
    settings.batch_max_prompt_tokens = 0
    settings.max_facts_per_message = 2
    settings.ingest_batch_concurrency = concurrency
    settings.language_detection_enabled = False
    settings.llm_outage_breaker_threshold = 100  # effectively disabled for error propagation tests
    return settings


def _make_runner_mock() -> MagicMock:
    async def _run_async(**kwargs):
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


@pytest.mark.asyncio
async def test_batch_exceptions_populate_result_errors() -> None:
    """Batches 2 and 4 crash; result.errors must have 2 entries, others succeed."""
    stores = _make_stores_mock()
    settings = _make_settings_mock(concurrency=4)
    session_svc = _make_session_service_mock()

    fake_session = MagicMock()
    fake_session.id = "sess-err"

    # Session state carries batch_num — use it to raise on batches 2 and 4.
    _sessions: dict[str, dict] = {}

    async def _fake_create_session(user_id, state):
        sess = MagicMock()
        sess.id = f"sess-{state.get('batch_num', 0)}"
        _sessions[sess.id] = state
        return sess

    async def _failing_run_async(user_id, session_id, new_message):
        state = _sessions.get(session_id, {})
        batch_num = state.get("batch_num", 0)
        if batch_num in (2, 4):
            raise RuntimeError(f"Simulated crash for batch {batch_num}")
        event = MagicMock()
        event.author = "persister"
        actions = MagicMock()
        actions.state_delta = {
            "persist_result": {"weaviate_ids": ["id1"], "entity_count": 1, "relationship_count": 0}
        }
        actions.stateDelta = None
        event.actions = actions

        async def _gen():
            yield event

        async for e in _gen():
            yield e

    runner = MagicMock()
    runner.run_async = _failing_run_async

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
            "beever_atlas.services.batch_processor.create_session", side_effect=_fake_create_session
        ),
        patch("beever_atlas.agents.runner.get_session_service", return_value=session_svc),
        patch("beever_atlas.services.batch_processor.get_llm_provider", return_value=MagicMock()),
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
    assert len(result.batch_breakdowns) == 4, (
        "All 4 breakdowns must be present (2 successful + 2 failure placeholders)"
    )


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
        messages = [{"text": f"msg{i}", "id": str(i)} for i in range(2)]
        result = await processor.process_messages(
            messages=messages,
            channel_id="C998",
            channel_name="ok-test",
            sync_job_id="job-no-errors-test",
        )

    assert len(result.errors) == 0
    assert len(result.batch_breakdowns) == 2


@pytest.mark.asyncio
async def test_truncated_json_into_cross_batch_validator_marks_failed_recoverable() -> None:
    """Simulates truncated JSON arriving at cross_batch_validator via state.

    When validated_entities is a truncated JSON string, wrap_with_recovery
    must set failed_recoverable=True and emit a truncation_report rather than
    crashing the pipeline.
    """
    from unittest.mock import MagicMock
    from beever_atlas.services.adk_recovery import wrap_with_recovery
    from beever_atlas.services.json_recovery import recover_validation_from_truncated
    from beever_atlas.agents.schemas.validation import ValidationResult

    # Build a minimal agent mock with output_key matching cross_batch_validator
    agent = MagicMock()
    agent.output_key = "validated_entities"
    agent.after_agent_callback = None

    wrap_with_recovery(agent, recover_validation_from_truncated, ValidationResult)

    # Simulate truncated JSON that cannot be fully recovered
    state: dict = {
        "validated_entities": '{"entities": [{"id": "e1", "name": "Alice", "type": "Person", "aliases": [], "attributes": {}, "source_message_ids": []}, {"id": "e2"'
        # truncated — missing closing braces/brackets
    }
    ctx = MagicMock()
    ctx.state = state

    agent.after_agent_callback(ctx)

    # The wrapper must either recover partial data OR mark failed_recoverable
    # Either way, the pipeline must not raise.
    validated = ctx.state.get("validated_entities")
    failed = ctx.state.get("failed_recoverable", False)

    # One of two valid outcomes:
    # 1. Recovery succeeded → validated_entities is a dict, failed_recoverable not set
    # 2. Recovery failed → failed_recoverable=True and truncation_report present
    if not failed:
        assert isinstance(validated, dict), (
            "If not failed_recoverable, validated_entities must be a dict"
        )
    else:
        report = ctx.state.get("truncation_report")
        assert report is not None, "failed_recoverable=True must come with a truncation_report"
        assert report["output_key"] == "validated_entities"
