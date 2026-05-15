"""Integration test for sync_summary: structured metrics.

Verifies that all 4 sync_summary: log lines are emitted exactly once at the
end of a process_messages() call, with the documented prefix + key=value format.

The project uses a structured JSON logger that bypasses caplog's root
propagation, so we spy on ``batch_processor.logger.info`` directly
(same technique as tests/unit/test_deterministic_cross_batch_validator.py).

Plan ref: .omc/plans/pipeline-realign-v2.md — Task 5 (PR-1), acceptance criterion 3.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import beever_atlas.services.batch_processor as _bp_mod
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
    stores.mongodb.increment_batches_completed_for_channel = AsyncMock(return_value=None)
    stores.mongodb.append_batch_results_for_channel = AsyncMock(return_value=None)
    stores.mongodb.finalize_extraction_status_bulk = AsyncMock(return_value=None)
    stores.mongodb.refresh_sync_progress_for_channel = AsyncMock(return_value=None)
    stores.entity_registry.get_all_canonical = AsyncMock(return_value=[])
    # Weaviate: return two clusters with member_count 1 and 3.
    _cl1 = MagicMock()
    _cl1.member_count = 1
    _cl2 = MagicMock()
    _cl2.member_count = 3
    stores.weaviate.list_clusters = AsyncMock(return_value=[_cl1, _cl2])
    return stores


def _make_settings_mock() -> MagicMock:
    settings = MagicMock()
    settings.sync_batch_size = 50
    settings.batch_max_prompt_tokens = 0
    settings.max_facts_per_message = 2
    settings.ingest_batch_concurrency = 1
    settings.language_detection_enabled = False
    settings.llm_outage_breaker_threshold = 100
    settings.neo4j_batch_name_vector = False
    return settings


def _make_runner_mock() -> MagicMock:
    """Runner that emits a single persister event with a minimal persist_result."""

    async def _run_async(**kwargs):
        event = MagicMock()
        event.author = "persister"
        actions = MagicMock()
        actions.state_delta = {
            "persist_result": {
                "weaviate_ids": ["wv-abc"],
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


def _make_session_service_mock() -> MagicMock:
    session = MagicMock()
    session.state = {
        "persist_result": {
            "weaviate_ids": ["wv-abc"],
            "entity_count": 1,
            "relationship_count": 0,
        },
        "extracted_facts": {"facts": []},
        "extracted_entities": {"entities": [], "relationships": []},
        "embedded_facts": [],
        "preprocessed_messages": [],
    }
    svc = MagicMock()
    svc.get_session = AsyncMock(return_value=session)
    return svc


async def _run_process(channel_id: str, sync_job_id: str) -> list[str]:
    """Run process_messages with mocked infrastructure; return captured info log messages."""
    stores = _make_stores_mock()
    settings = _make_settings_mock()
    session_svc = _make_session_service_mock()
    fake_session = MagicMock()
    fake_session.id = f"sess-{sync_job_id}"

    messages = [{"message_id": "m1", "content": "hello world", "timestamp": None}]

    captured: list[str] = []
    original_info = _bp_mod.logger.info

    def _spy_info(msg, *args, **kwargs):
        text = msg % args if args else str(msg)
        captured.append(text)
        return original_info(msg, *args, **kwargs)

    with (
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
        patch("beever_atlas.services.batch_processor.get_settings", return_value=settings),
        patch(
            "beever_atlas.services.batch_processor.create_runner",
            return_value=_make_runner_mock(),
        ),
        patch(
            "beever_atlas.services.batch_processor.create_ingestion_pipeline",
            return_value=MagicMock(),
        ),
        patch(
            "beever_atlas.services.batch_processor.create_session",
            return_value=AsyncMock(return_value=fake_session),
        ),
        patch("beever_atlas.agents.runner.get_session_service", return_value=session_svc),
        patch.object(_bp_mod.logger, "info", side_effect=_spy_info),
    ):
        processor = BatchProcessor()
        await processor.process_messages(
            messages=messages,
            channel_id=channel_id,
            channel_name="test-channel",
            sync_job_id=sync_job_id,
        )

    return captured


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_summary_lines_emitted_once() -> None:
    """All 4 sync_summary: lines emit exactly once at sync completion."""
    all_msgs = await _run_process("ch-test-123", "job-test-456")

    summary_lines = [m for m in all_msgs if m.startswith("sync_summary:")]

    assert len(summary_lines) == 4, (
        f"Expected 4 sync_summary: lines, got {len(summary_lines)}. Lines:\n"
        + "\n".join(summary_lines)
    )

    metrics_found = {
        "relationships_dropped_total": False,
        "cluster_size_histogram": False,
        "entity_truncation_recoveries": False,
        "cross_batch_validator_llm_fallback_total": False,
    }

    for msg in summary_lines:
        # Prefix must be intact
        assert msg.startswith("sync_summary: "), f"Bad prefix: {msg!r}"
        # channel_id and sync_job_id must be present
        assert "channel_id=ch-test-123" in msg, f"Missing channel_id in: {msg!r}"
        assert "sync_job_id=job-test-456" in msg, f"Missing sync_job_id in: {msg!r}"
        # metric= key must be present
        m = re.search(r"metric=(\S+)", msg)
        assert m, f"No metric= key in: {msg!r}"
        metric_name = m.group(1)
        assert metric_name in metrics_found, f"Unexpected metric name: {metric_name!r}"
        metrics_found[metric_name] = True

    for name, found in metrics_found.items():
        assert found, f"sync_summary metric not emitted: {name}"


@pytest.mark.asyncio
async def test_sync_summary_format_relationships_dropped() -> None:
    """relationships_dropped_total line has value=<int> format."""
    all_msgs = await _run_process("ch-fmt", "job-fmt")

    rels_lines = [m for m in all_msgs if "metric=relationships_dropped_total" in m]
    assert len(rels_lines) == 1, (
        f"Expected 1 relationships_dropped_total line, got {len(rels_lines)}"
    )
    line = rels_lines[0]
    m = re.search(r"value=(\d+)", line)
    assert m, f"No value=<int> in: {line!r}"
    assert int(m.group(1)) >= 0


@pytest.mark.asyncio
async def test_sync_summary_cluster_histogram_format() -> None:
    """cluster_size_histogram line has value=[...] JSON-array format."""
    all_msgs = await _run_process("ch-hist", "job-hist")

    hist_lines = [m for m in all_msgs if "metric=cluster_size_histogram" in m]
    assert len(hist_lines) == 1, f"Expected 1 cluster_size_histogram line, got {len(hist_lines)}"
    line = hist_lines[0]
    m = re.search(r"value=(\[.*?\])", line)
    assert m, f"No value=[...] array in: {line!r}"


@pytest.mark.asyncio
async def test_sync_metrics_no_cross_channel_leak() -> None:
    """Counter state for channel A does not bleed into channel B."""
    from beever_atlas.services.batch_processor import (
        _drain_sync_metrics,
        _init_sync_metrics,
        increment_sync_metric,
    )

    _init_sync_metrics("ch-A", "job-1")
    _init_sync_metrics("ch-B", "job-2")

    increment_sync_metric("ch-A", "job-1", "relationships_dropped_total", 5)
    increment_sync_metric("ch-B", "job-2", "relationships_dropped_total", 0)

    metrics_a = _drain_sync_metrics("ch-A", "job-1")
    metrics_b = _drain_sync_metrics("ch-B", "job-2")

    assert metrics_a.get("relationships_dropped_total") == 5
    assert metrics_b.get("relationships_dropped_total") == 0

    # After drain, further increments are silently no-ops (bucket closed).
    increment_sync_metric("ch-A", "job-1", "relationships_dropped_total", 99)
    drained_again = _drain_sync_metrics("ch-A", "job-1")
    assert drained_again == {}
