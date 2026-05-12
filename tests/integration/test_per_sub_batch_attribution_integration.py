"""Integration test for per-sub-batch failure attribution (B1 / Task 2.1.6).

Drives a real :class:`BatchProcessor` end-to-end with the ADK runner and
session service mocked at the module level so we can simulate a 429 on
exactly one sub-batch out of many. The full BatchResult assembly path
runs (no shortcuts) and we assert:

  1. ``result.batch_breakdowns`` contains exactly one entry with
     ``error != None``.
  2. The successful breakdowns carry their ``keys`` populated.
  3. ``len(succeeded_keys) + len(failed_keys) == total_messages`` —
     the partition invariant the worker relies on.

This is the integration counterpart to the unit tests in
``tests/services/test_extraction_worker_per_sub_batch_attribution.py``;
together they lock in decision D1 (sub-batch granularity).
"""

from __future__ import annotations

import asyncio
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


def _make_settings_mock(*, batch_size: int = 25, concurrency: int = 2) -> MagicMock:
    settings = MagicMock()
    settings.sync_batch_size = batch_size
    settings.batch_max_prompt_tokens = 0  # disable adaptive batcher
    settings.batch_max_output_tokens = 0
    settings.batch_time_window_seconds = 60
    settings.batch_max_messages = batch_size
    settings.max_facts_per_message = 1
    settings.ingest_batch_concurrency = concurrency
    settings.language_detection_enabled = False
    settings.llm_outage_breaker_threshold = 100  # effectively disabled
    settings.entity_threshold = 0.0
    settings.quality_threshold = 0.0
    settings.default_target_language = "en"
    settings.language_detection_confidence_threshold = 0.5
    return settings


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


def _runner_factory(fail_on_batch: int) -> MagicMock:
    """Build a runner whose ``run_async`` raises only on a specific batch.

    The BatchProcessor sets ``_batch_idx_var`` to the batch index inside
    the semaphore-guarded body, then calls ``runner.run_async`` to drive
    the ADK pipeline. We read that ContextVar from inside the mock to
    decide whether to raise.
    """
    from beever_atlas.services.batch_processor import _batch_idx_var

    async def _run_async(**kwargs):  # pragma: no cover — driven by BatchProcessor
        idx = _batch_idx_var.get()
        if idx == fail_on_batch:
            # Yield nothing, then raise — mirrors a Gemini 429 mid-stream.
            await asyncio.sleep(0)
            raise RuntimeError(f"litellm 429 RateLimitError on batch {idx}")
        # Healthy path: yield one persister event so the breakdown gets
        # populated end-to-end.
        event = MagicMock()
        event.author = "persister"
        actions = MagicMock()
        actions.state_delta = {
            "persist_result": {
                "weaviate_ids": [f"id-{idx}"],
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


@pytest.mark.asyncio
async def test_one_sub_batch_429_others_succeed() -> None:
    """200 messages, 8 sub-batches of 25; sub-batch 3 raises 429.

    The other 7 sub-batches must succeed and carry their per-sub-batch
    keys, so a downstream consumer (the ExtractionWorker) can split
    success vs failure with no row leakage.
    """
    stores = _make_stores_mock()
    settings = _make_settings_mock(batch_size=25, concurrency=2)
    runner = _runner_factory(fail_on_batch=3)
    session_svc = _make_session_service_mock()

    fake_session = MagicMock()
    fake_session.id = "sess-integration"

    processor = BatchProcessor()

    # 200 messages → 8 batches of 25 with batch_size=25 and the
    # thread-aware batcher (no thread_id set, so simple fixed split).
    messages = [
        {
            "text": f"hello {i}",
            "channel_id": "C-int",
            "channel_name": "general",
            "message_id": f"m{i:03d}",
            "source_id": "src",
            "platform": "src",
            "author": "alice",
            "author_name": "Alice",
            "timestamp": "2026-05-01T00:00:00Z",
        }
        for i in range(200)
    ]

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
        patch(
            "beever_atlas.services.batch_processor.get_llm_provider",
            return_value=MagicMock(),
        ),
    ):
        result = await processor.process_messages(
            messages=messages,
            channel_id="C-int",
            channel_name="test",
            sync_job_id="job-integration",
        )

    # 8 sub-batches expected.
    assert len(result.batch_breakdowns) == 8

    # Exactly one breakdown carries an error.
    failed_breakdowns = [bd for bd in result.batch_breakdowns if bd.error is not None]
    assert len(failed_breakdowns) == 1, (
        f"Expected exactly one failing sub-batch, got {len(failed_breakdowns)}: "
        f"{[bd.error for bd in failed_breakdowns]}"
    )

    # The error path must still carry sub-batch keys so the worker can
    # attribute failure correctly. Without this, partitions leak.
    assert failed_breakdowns[0].keys, (
        "failing sub-batch's BatchBreakdown.keys must be populated "
        "(otherwise the worker falls through to the legacy all-or-nothing path)"
    )

    # Every successful breakdown also carries keys.
    succeeded_breakdowns = [bd for bd in result.batch_breakdowns if bd.error is None]
    assert all(bd.keys for bd in succeeded_breakdowns), (
        "every BatchBreakdown must carry its sub-batch keys"
    )

    # The partition invariant: succeeded_keys + failed_keys == 200.
    succeeded_keys: list = []
    failed_keys: list = []
    for bd in result.batch_breakdowns:
        if bd.error is None:
            succeeded_keys.extend(bd.keys)
        else:
            failed_keys.extend(bd.keys)

    assert len(succeeded_keys) + len(failed_keys) == 200, (
        f"succeeded({len(succeeded_keys)}) + failed({len(failed_keys)}) "
        f"must equal total messages (200)"
    )

    # And the keys are disjoint (no row both succeeded and failed).
    assert set(succeeded_keys).isdisjoint(set(failed_keys)), "succeeded ∩ failed must be empty"
