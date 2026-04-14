"""Tests for unconditional checkpoint resume on retry in BatchProcessor.

Verifies that on any retry (regardless of exception class), BatchProcessor
re-consults the checkpoint store so that already-completed stages (fact
extraction, entity extraction) are not re-run.

Phase 1 Step 2 — ingestion-pipeline-hardening plan.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, call

import httpx
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response_500() -> httpx.Response:
    """Build a minimal httpx.Response with status 500."""
    response = httpx.Response(
        status_code=500,
        content=b"Internal Server Error",
        request=httpx.Request("POST", "https://embed.example.com/embed"),
    )
    return response


def _make_http_status_error() -> httpx.HTTPStatusError:
    response = _make_response_500()
    return httpx.HTTPStatusError(
        "500 Internal Server Error", request=response.request, response=response
    )


def _make_mock_stores(*, checkpoint_on_retry: dict | None = None):
    """Return a mock StoreClients with configurable checkpoint behavior."""
    mongodb = AsyncMock()
    mongodb.update_sync_progress = AsyncMock(return_value=None)
    mongodb.load_pipeline_checkpoint = AsyncMock(return_value=None)
    mongodb.delete_pipeline_checkpoint = AsyncMock(return_value=None)
    mongodb.save_pipeline_checkpoint = AsyncMock(return_value=None)

    entity_registry = AsyncMock()
    entity_registry.get_all_canonical = AsyncMock(return_value=[])

    stores = MagicMock()
    stores.mongodb = mongodb
    stores.entity_registry = entity_registry
    return stores


def _make_mock_settings():
    return SimpleNamespace(
        sync_batch_size=10,
        batch_max_prompt_tokens=0,  # disable adaptive batching
        max_facts_per_message=2,
        ingest_batch_concurrency=1,
        language_detection_enabled=False,
        default_target_language="en",
        language_detection_confidence_threshold=0.8,
    )


def _make_session(session_id: str = "sess-1"):
    session = MagicMock()
    session.id = session_id
    session.state = {
        "persist_result": {
            "facts_count": 1,
            "entity_count": 0,
            "relationship_count": 0,
            "weaviate_ids": ["wv-1"],
        },
        "embedded_facts": [{"memory_text": "test fact", "message_ts": "123"}],
        "extracted_facts": {"facts": [{"memory_text": "test fact"}]},
        "extracted_entities": {"entities": [], "relationships": []},
    }
    return session


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_checkpoint_reloaded_on_httpx_error_retry():
    """On retry after httpx.HTTPStatusError, load_pipeline_checkpoint is called again.

    This ensures the retry picks up the checkpoint and skips already-completed
    stages (fact extraction, entity extraction) rather than restarting from Stage 1.
    """
    from beever_atlas.services.batch_processor import BatchProcessor

    stores = _make_mock_stores()
    settings = _make_mock_settings()
    session = _make_session()

    # Simulate: attempt 0 raises httpx.HTTPStatusError (embedder 500),
    # attempt 1 (retry) succeeds.
    attempt_count = 0

    async def _fake_run_async(**kwargs):
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise _make_http_status_error()
        # Second attempt: yield one event and complete cleanly
        yield SimpleNamespace(author="persister", actions=None)

    mock_runner = MagicMock()
    mock_runner.run_async = _fake_run_async

    mock_final_session = MagicMock()
    mock_final_session.state = session.state

    mock_session_service = AsyncMock()
    mock_session_service.get_session = AsyncMock(return_value=mock_final_session)

    messages = [{"text": "hello", "ts": "1000.0", "user": "U1"}]

    with (
        patch(
            "beever_atlas.services.batch_processor.get_stores",
            return_value=stores,
        ),
        patch(
            "beever_atlas.services.batch_processor.get_settings",
            return_value=settings,
        ),
        patch(
            "beever_atlas.services.batch_processor.create_ingestion_pipeline",
            return_value=MagicMock(),
        ),
        patch(
            "beever_atlas.services.batch_processor.create_runner",
            return_value=mock_runner,
        ),
        patch(
            "beever_atlas.services.batch_processor.create_session",
            return_value=session,
        ),
        patch(
            "beever_atlas.services.batch_processor.get_llm_provider",
            return_value=MagicMock(),
        ),
        patch(
            "beever_atlas.agents.runner.get_session_service",
            return_value=mock_session_service,
        ),
        patch("asyncio.sleep", new=AsyncMock()),  # skip retry backoff
    ):
        processor = BatchProcessor()
        result = await processor.process_messages(
            messages=messages,
            channel_id="C123",
            channel_name="test-channel",
            sync_job_id="job-abc",
        )

    # load_pipeline_checkpoint must be called at least twice:
    # once before the retry loop (attempt 0), once inside the retry (attempt 1).
    load_calls = stores.mongodb.load_pipeline_checkpoint.call_args_list
    assert len(load_calls) >= 2, (
        f"Expected load_pipeline_checkpoint called >=2 times on retry, got {len(load_calls)}. "
        "This means checkpoint resume is still conditional on exception class."
    )

    # All calls must use the same job_id and batch_num.
    for c in load_calls:
        assert c.kwargs.get("sync_job_id") == "job-abc" or c.args[0] == "job-abc"

    # The pipeline eventually succeeded (no errors in result).
    assert result.errors == [], f"Unexpected errors: {result.errors}"
