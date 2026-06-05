"""Issue #223 — transient-net-error resilience + ingestion SSE streaming.

Covers the two-layer fix that stops a long, non-streaming gemini-2.5-pro call
from dropping at the ~127-131s idle-proxy ceiling and cascading (via the circuit
breaker) into ``rows=N succeeded=0 total_facts=0``:

Layer 1 (ROOT) — when ``INGEST_ADK_STREAMING_SSE`` is on, the ingestion runner
receives ``RunConfig(streaming_mode=StreamingMode.SSE)`` so ADK streams the call
and the socket never idles to the disconnect threshold.

Layer 2 (ROBUSTNESS) — ``_is_transient_net_error`` classifies a clean idle
disconnect; such drops retry with the existing backoff ladder and, critically,
are NEVER fed to the circuit breaker, so a disconnect storm can no longer open
the breaker and fast-fail the rest of the job.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import httpx
import pytest
from google.adk.agents.run_config import StreamingMode
from google.genai.errors import ServerError
from pydantic import BaseModel, ValidationError

from beever_atlas.services.batch_processor import (
    BatchProcessor,
    _is_transient_net_error,
    _is_truncation_error,
)


# ---------------------------------------------------------------------------
# get_settings lru_cache fragility — rebuild Settings around every test so a
# fresh module that reads get_settings() at import/collection time can't pin a
# stale cache. Mirrors the sibling media tests' fixture.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _fresh_settings():
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class _Model(BaseModel):
    x: int


# ===========================================================================
# (a) _is_transient_net_error truthiness matrix
# ===========================================================================
class TestIsTransientNetError:
    def test_server_disconnected_error(self):
        assert _is_transient_net_error(aiohttp.ServerDisconnectedError()) is True

    def test_client_connection_error(self):
        assert _is_transient_net_error(aiohttp.ClientConnectionError()) is True

    def test_client_os_error(self):
        assert _is_transient_net_error(aiohttp.ClientOSError()) is True

    def test_connection_reset_error(self):
        assert _is_transient_net_error(ConnectionResetError()) is True

    def test_httpx_remote_protocol_error_is_not_transient(self):
        # Ambiguous with mid-stream truncation — routed to the truncation
        # reduce→halve ladder, NOT the transient checkpoint-replay retry.
        err = httpx.RemoteProtocolError("peer closed")
        assert _is_transient_net_error(err) is False
        assert _is_truncation_error(err) is True

    def test_httpx_read_error_is_not_transient(self):
        assert _is_transient_net_error(httpx.ReadError("read failed")) is False

    def test_timeout_error(self):
        assert _is_transient_net_error(TimeoutError()) is True

    def test_asyncio_timeout_error(self):
        assert _is_transient_net_error(asyncio.TimeoutError()) is True

    def test_exception_group_wrapping_transient(self):
        # ADK/anyio TaskGroups wrap the real cause in an ExceptionGroup; the
        # predicate must recurse into .exceptions and report True.
        eg = ExceptionGroup("task group", [aiohttp.ServerDisconnectedError()])
        assert _is_transient_net_error(eg) is True

    def test_exception_group_nested(self):
        eg = ExceptionGroup(
            "outer",
            [ExceptionGroup("inner", [ConnectionResetError()])],
        )
        assert _is_transient_net_error(eg) is True

    def test_server_error_is_not_transient(self):
        # Genuine provider 5xx — must still count toward the breaker, so NOT
        # classified as a transient net drop.
        assert _is_transient_net_error(ServerError(503, {"error": "x"})) is False

    def test_validation_error_is_not_transient(self):
        try:
            _Model(x="not-an-int")  # type: ignore[arg-type]
        except ValidationError as exc:
            assert _is_transient_net_error(exc) is False

    def test_json_decode_error_is_not_transient(self):
        try:
            json.loads("{")
        except json.JSONDecodeError as exc:
            assert _is_transient_net_error(exc) is False

    def test_exception_group_of_non_transient_is_false(self):
        eg = ExceptionGroup("g", [ServerError(503, {"error": "x"})])
        assert _is_transient_net_error(eg) is False


# ---------------------------------------------------------------------------
# Full-flow harness helpers (drive BatchProcessor.process_messages with mocks).
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
    settings.llm_outage_breaker_threshold = 3
    settings.defer_contradiction = True
    settings.ingest_adk_streaming_sse = False
    return settings


def _success_event() -> MagicMock:
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
    return event


def _final_session_with_result() -> MagicMock:
    sess = MagicMock()
    sess.state = {
        "persist_result": {
            "weaviate_ids": ["id1"],
            "entity_count": 1,
            "relationship_count": 0,
        }
    }
    return sess


class _SpyBreaker:
    """Records record_failure / record_success calls; never opens.

    Lets a test prove that transient drops do NOT advance the breaker while a
    genuine ServerError does.
    """

    def __init__(self) -> None:
        self.failure_calls: list[BaseException | None] = []
        self.success_calls = 0

    async def allow(self) -> bool:
        return True

    async def record_failure(self, exc: BaseException | None = None) -> None:
        self.failure_calls.append(exc)

    async def record_success(self) -> None:
        self.success_calls += 1

    def snapshot(self) -> MagicMock:
        snap = MagicMock()
        snap.consecutive_failures = len(self.failure_calls)
        snap.threshold = 3
        snap.state = "closed"
        return snap


async def _run_processor(processor: BatchProcessor, runner: MagicMock, settings: MagicMock):
    """Drive process_messages with a fully mocked environment."""
    stores = _make_stores_mock()
    fake_session = MagicMock()
    fake_session.id = "sess-1"

    session_service = MagicMock()
    session_service.get_session = AsyncMock(return_value=_final_session_with_result())

    async def _no_sleep(_secs: float) -> None:
        return None

    with (
        patch("beever_atlas.services.batch_processor.random.uniform", return_value=0.0),
        patch(
            "beever_atlas.services.batch_processor.asyncio.sleep",
            side_effect=_no_sleep,
        ),
        patch("beever_atlas.services.batch_processor.get_stores", return_value=stores),
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
            return_value=runner,
        ),
        patch(
            "beever_atlas.services.batch_processor.create_session",
            new=AsyncMock(return_value=fake_session),
        ),
        patch(
            "beever_atlas.services.batch_processor.get_llm_provider",
            return_value=MagicMock(),
        ),
        patch(
            "beever_atlas.agents.runner.get_session_service",
            return_value=session_service,
        ),
    ):
        return await processor.process_messages(
            messages=[{"content": "hello", "message_id": "msg-1", "channel_id": "C123"}],
            channel_id="C123",
            channel_name="test",
            sync_job_id="job-223",
        )


# ===========================================================================
# (b) ServerDisconnected on attempt 0 → retry (continue), not immediate raise
# ===========================================================================
@pytest.mark.asyncio
async def test_server_disconnected_triggers_retry_not_immediate_raise():
    """First attempt disconnects, second succeeds → run_async called twice."""
    settings = _make_settings_mock()
    breaker = _SpyBreaker()
    call_count = 0

    async def _run_async(**_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise aiohttp.ServerDisconnectedError()
        yield _success_event()

    runner = MagicMock()
    runner.run_async = _run_async

    processor = BatchProcessor(breaker=breaker)  # type: ignore[arg-type]
    result = await _run_processor(processor, runner, settings)

    # Retried: run_async invoked more than once (disconnect → backoff → success).
    assert call_count >= 2
    # Transient drop must NEVER feed the breaker.
    assert breaker.failure_calls == []
    # Batch ultimately succeeded → the persisted fact id from the success event
    # reaches the result (fact_ids is the reliable signal under minimal mocks).
    assert result.fact_ids == ["id1"]
    # And the row did NOT surface as an error (the disconnect was absorbed).
    assert result.errors == []


# ===========================================================================
# (c) Terminal transient exhaustion does NOT call breaker.record_failure,
#     while a genuine ServerError DOES — proving a disconnect storm can't open
#     the breaker.
# ===========================================================================
@pytest.mark.asyncio
async def test_transient_exhaustion_never_feeds_breaker():
    settings = _make_settings_mock()
    breaker = _SpyBreaker()

    async def _always_disconnect(**_kwargs):
        raise aiohttp.ServerDisconnectedError()
        yield  # pragma: no cover — makes this an async generator

    runner = MagicMock()
    runner.run_async = _always_disconnect

    processor = BatchProcessor(breaker=breaker)  # type: ignore[arg-type]
    result = await _run_processor(processor, runner, settings)

    # Row failed (no facts) ...
    assert result.total_facts == 0
    assert result.errors
    # ... but the breaker was NEVER advanced by the transient drops.
    assert breaker.failure_calls == []


@pytest.mark.asyncio
async def test_server_error_exhaustion_does_feed_breaker():
    """Control: a genuine ServerError DOES advance the breaker on exhaustion."""
    settings = _make_settings_mock()
    breaker = _SpyBreaker()

    async def _always_5xx(**_kwargs):
        raise ServerError(503, {"error": "upstream down"})
        yield  # pragma: no cover

    runner = MagicMock()
    runner.run_async = _always_5xx

    processor = BatchProcessor(breaker=breaker)  # type: ignore[arg-type]
    await _run_processor(processor, runner, settings)

    # ServerError is resumable; on terminal exhaustion it records exactly one
    # failure toward the breaker.
    assert len(breaker.failure_calls) == 1
    assert isinstance(breaker.failure_calls[0], ServerError)


# ===========================================================================
# (d) Streaming flag wiring — run_async receives run_config with SSE.
# ===========================================================================
@pytest.mark.asyncio
async def test_streaming_flag_passes_sse_run_config():
    settings = _make_settings_mock()
    settings.ingest_adk_streaming_sse = True
    breaker = _SpyBreaker()
    captured: dict[str, object] = {}

    async def _run_async(**kwargs):
        captured.update(kwargs)
        yield _success_event()

    runner = MagicMock()
    runner.run_async = _run_async

    processor = BatchProcessor(breaker=breaker)  # type: ignore[arg-type]
    await _run_processor(processor, runner, settings)

    assert "run_config" in captured
    assert captured["run_config"].streaming_mode == StreamingMode.SSE  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_streaming_flag_off_omits_run_config():
    settings = _make_settings_mock()
    settings.ingest_adk_streaming_sse = False
    breaker = _SpyBreaker()
    captured: dict[str, object] = {}

    async def _run_async(**kwargs):
        captured.update(kwargs)
        yield _success_event()

    runner = MagicMock()
    runner.run_async = _run_async

    processor = BatchProcessor(breaker=breaker)  # type: ignore[arg-type]
    await _run_processor(processor, runner, settings)

    # When the flag is off, ADK keeps its own default RunConfig (non-streaming):
    # we must NOT pass run_config at all.
    assert "run_config" not in captured
