"""Batch processor — chunks messages and drives them through the ingestion pipeline.

Splits a list of NormalizedMessage objects into fixed-size batches, runs each
batch through the ADK ``ingestion_pipeline`` Workflow, and accumulates
per-batch results into a final ``BatchResult``.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

import aiohttp
import httpx
import json
from aiolimiter import AsyncLimiter
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types
from google.genai.errors import ServerError
from pydantic import ValidationError as PydanticValidationError

from beever_atlas.agents.ingestion import create_ingestion_pipeline
from beever_atlas.models.sync_policy import IngestionConfig
from beever_atlas.agents.runner import create_runner, create_session
from beever_atlas.infra.config import get_settings
from beever_atlas.stores import get_stores
from beever_atlas.llm import get_llm_provider
from beever_atlas.services.pipeline_events import get_pipeline_events

logger = logging.getLogger(__name__)

# ── Per-provider rate limiters (requests per minute) ─────────────────────────
# Lazily initialised on first use so tests can patch get_settings() before the
# module is imported. The lock guards one-time creation.
_limiter_lock = asyncio.Lock()
_provider_limiters: dict[str, AsyncLimiter] = {}


async def _get_limiter(provider: str) -> AsyncLimiter:
    """Return the AsyncLimiter for *provider*, creating it once from settings.

    Provider keys: ``"gemini"`` (chat / extraction), ``"embedding"`` (the
    provider-agnostic embedding shim). The legacy key ``"jina"`` is mapped
    to ``"embedding"`` for one release so out-of-tree callers keep working.
    """
    # Legacy alias — remove once external callers update.
    if provider == "jina":
        provider = "embedding"
    if provider not in _provider_limiters:
        async with _limiter_lock:
            if provider not in _provider_limiters:
                cfg = get_settings()
                rpm = cfg.gemini_rpm if provider == "gemini" else cfg.embedding_rpm
                _provider_limiters[provider] = AsyncLimiter(rpm, 60)
    return _provider_limiters[provider]


# ``_consecutive_503_count`` and ``_consecutive_503_lock`` have been removed —
# the breaker now lives in ``services/circuit_breaker.py`` and is injected
# via :class:`BatchProcessor.__init__`. Tests that previously depended on
# resetting these module-globals can drop their workaround fixture.

# ContextVar so callbacks in workers 2/3 can read the current batch index.
_batch_idx_var: contextvars.ContextVar[int] = contextvars.ContextVar("batch_idx", default=0)


class ProviderOutageError(Exception):
    """Raised when consecutive cross-batch 5xx from Gemini exceeds the configured threshold."""


# ─────────────────────────────────────────────────────────────────────────────

_LLM_MAX_RETRIES = 5
# First retry intentionally short (5s) — transient 5xx/network blips
# recover almost immediately. Subsequent retries climb sharply for
# real rate-limit windows (Gemini quotas reset on per-minute boundary).
# Earlier schedule [30, 60, 120, 240, 480] added 30s of dead time to
# EVERY transient hiccup, causing the per-batch max latency observed
# in scripts/test_pipeline_design.py to balloon to 144s on one outlier.
_LLM_RETRY_BACKOFF = [5, 30, 90, 180, 360]  # seconds between retries

# Map ADK agent names to human-readable stage descriptions with step numbers.
_STAGE_LABELS: dict[str, str] = {
    "preprocessor": "Step 1/6 — Preprocessing messages",
    "fact_extractor": "Step 2/6 — Extracting facts (LLM)",
    "entity_extractor": "Step 3/6 — Extracting entities (LLM)",
    "embedder": "Step 4/6 — Generating embeddings",
    "cross_batch_validator_agent": "Step 5/6 — Validating entities",
    "persister": "Step 6/6 — Saving to stores",
}

_STAGE_ORDER: list[str] = [
    "preprocessor",
    "fact_extractor",
    "entity_extractor",
    "embedder",
    "cross_batch_validator_agent",
    "persister",
]

_ALL_CHECKPOINT_KEYS: list[str] = [
    "preprocessed_messages",
    "extracted_facts",
    "extracted_entities",
    "classified_facts",
    "embedded_facts",
    "validated_entities",
]


def _thread_aware_batches(messages: list[Any], batch_size: int) -> list[list[Any]]:
    """Split messages into batches, keeping thread groups (parent + replies) intact.

    Messages are expected to have replies inserted adjacent to their parent
    by ``SyncRunner._fetch_thread_replies``. This function never splits a
    parent from its replies across batches. Batches may slightly exceed
    ``batch_size`` to keep a thread group together.
    """
    if not messages:
        return []

    batches: list[list[Any]] = []
    current_batch: list[Any] = []

    for msg in messages:
        thread_id = getattr(msg, "thread_id", None)
        if isinstance(msg, dict):
            thread_id = msg.get("thread_id")

        is_reply = bool(thread_id)

        if not is_reply and len(current_batch) >= batch_size:
            # Start a new batch at a top-level message boundary
            batches.append(current_batch)
            current_batch = []

        current_batch.append(msg)

    if current_batch:
        batches.append(current_batch)

    # Log warning for oversized batches
    for i, batch in enumerate(batches):
        if len(batch) > 2 * batch_size:
            logger.warning(
                "BatchProcessor: batch %d has %d messages (>2x batch_size=%d) "
                "due to large thread group",
                i + 1,
                len(batch),
                batch_size,
            )

    return batches


def _summarize_exception(exc: Exception) -> str:
    """Create a compact, actionable error message for logs and sync status."""
    if isinstance(exc, ExceptionGroup):
        parts: list[str] = []
        for sub in exc.exceptions:
            parts.append(f"{type(sub).__name__}: {sub}")
        return "; ".join(parts)
    return str(exc)


def _is_truncation_error(exc: Exception) -> bool:
    """Return True if ``exc`` indicates an LLM output truncation / malformed JSON.

    Covers: Pydantic ValidationError, json.JSONDecodeError, ijson IncompleteJSONError,
    httpx RemoteProtocolError, plus string markers from Gemini/ADK (``json_invalid``,
    ``max_tokens``, ``unexpected eof``). Retry ladder in the main loop uses this
    predicate as its trigger — existing ladder body (reduce → halve → raise) unchanged.
    """
    name = type(exc).__name__
    msg = str(exc).lower()
    if name in {
        "ValidationError",
        "JSONDecodeError",
        "IncompleteJSONError",
        "RemoteProtocolError",
    }:
        return True
    return any(marker in msg for marker in ("json_invalid", "max_tokens", "unexpected eof"))


def _source_id_of(msg: Any) -> str:
    """Best-effort extraction of source_id from a NormalizedMessage or dict.

    Mirrors the worker's reverse mapping in ``extraction_worker.py``: pull
    a stable source identifier from ``platform`` (NormalizedMessage) or
    ``source_id``/``platform`` (dict). Returns empty string when neither
    is present so the (source_id, channel_id, message_id) triple still
    matches the worker's claimed-doc keys.
    """
    if isinstance(msg, dict):
        return str(msg.get("source_id") or msg.get("platform") or "")
    return str(getattr(msg, "platform", "") or getattr(msg, "source_id", "") or "")


def _keys_for_batch(batch: list[Any]) -> list[tuple[str, str, str]]:
    """Build the per-sub-batch ``(source_id, channel_id, message_id)`` keys.

    Used to populate ``BatchBreakdown.keys`` so the ExtractionWorker can
    attribute success or failure per sub-batch (decision D1).
    """
    keys: list[tuple[str, str, str]] = []
    for m in batch:
        if isinstance(m, dict):
            keys.append(
                (
                    _source_id_of(m),
                    str(m.get("channel_id") or ""),
                    str(m.get("message_id") or ""),
                )
            )
        else:
            keys.append(
                (
                    _source_id_of(m),
                    str(getattr(m, "channel_id", "") or ""),
                    str(getattr(m, "message_id", "") or ""),
                )
            )
    return keys


def _is_resumable(exc: Exception) -> bool:
    """Return True if ``exc`` should trigger checkpoint-aware retry.

    These exception types warrant a full retry from the last checkpoint rather
    than the truncation-reduce-halve path: provider 5xx (ServerError, HTTP 5xx),
    pydantic ValidationError (malformed LLM JSON), and json.JSONDecodeError.

    EXCEPTION: a ``PydanticValidationError`` whose error string identifies a
    DETERMINISTIC schema mismatch (LLM returned a wrongly-typed scalar like
    ``None`` for a required ``str`` field) is NOT resumable — replaying the
    checkpoint just feeds the same bad data back in and fails the same way.
    The retry chain wastes ~11 min per such error before giving up. We catch
    the prefix ``Input should be a valid``/``Input should be a string`` here
    and bail out immediately. The persister's downstream coercion handles
    the empty-result case.
    """
    if isinstance(exc, PydanticValidationError):
        msg = str(exc)
        if "Input should be a valid" in msg or "Input should be a string" in msg:
            return False
        return True
    if isinstance(exc, ServerError):
        return True
    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
        return True
    if isinstance(exc, json.JSONDecodeError):
        return True
    return False


def _is_transient_net_error(exc: BaseException) -> bool:
    """Return True if ``exc`` is a transient connection-level network drop.

    Issue #223: a long, non-streaming ``gemini-2.5-pro`` ``generate_content``
    call sits idle past the ~127-131s edge-proxy ceiling and the peer closes
    the idle socket → ``aiohttp.ServerDisconnectedError``. This is NONE of the
    four types caught at the resumable-retry site (``ServerError`` / 5xx
    ``httpx.HTTPStatusError`` / ``PydanticValidationError`` / ``JSONDecodeError``)
    and ``_is_truncation_error`` returns False for it, so without this predicate
    it would hit the terminal ``else: raise`` on the very FIRST attempt with no
    backoff. Worse, concurrent batches' disconnects feed the circuit breaker and
    open it, fast-failing every remaining sub-batch into ``total_facts=0``.

    Matches: aiohttp ServerDisconnectedError / ClientConnectionError /
    ClientOSError, the stdlib ConnectionResetError, httpx RemoteProtocolError /
    ReadError, and asyncio/builtin TimeoutError. ADK/anyio TaskGroups wrap the
    real cause in an ``ExceptionGroup`` (``BaseExceptionGroup``), so we recurse
    into ``.exceptions`` and report True if ANY leaf is a transient net error.

    NOTE: ``httpx.RemoteProtocolError`` / ``ReadError`` are deliberately NOT
    matched here — they are ambiguous (a mid-stream truncation surfaces the same
    way) and are already handled by ``_is_truncation_error``'s reduce→halve
    ladder. The genuine Issue #223 drop is the aiohttp ``ServerDisconnectedError``
    on the google-genai aio path, so keeping the httpx truncation routing
    unchanged avoids replaying an oversized request without shrinking it.
    """
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_transient_net_error(sub) for sub in exc.exceptions)
    if isinstance(
        exc,
        (
            aiohttp.ServerDisconnectedError,
            aiohttp.ClientConnectionError,
            aiohttp.ClientOSError,
            ConnectionResetError,
            asyncio.TimeoutError,
            TimeoutError,
        ),
    ):
        return True
    return False


@dataclass
class BatchBreakdown:
    """Per-batch extraction breakdown with sample data."""

    batch_num: int = 0
    facts_count: int = 0
    entities_count: int = 0
    relationships_count: int = 0
    # Counts surfaced by the embedder and preprocessor stages — populated
    # by batch_pipeline.py from the same stage_output metrics that feed
    # the UI's activity_log. Frontend MetricsBar reads these when the
    # server-side ``batch_results`` array is preferred over the sticky
    # client-side accumulator.
    embedded_count: int = 0
    media_count: int = 0
    sample_facts: list[str] = field(default_factory=list)
    sample_entities: list[dict[str, str]] = field(default_factory=list)
    sample_relationships: list[dict[str, str]] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None
    # Fact-level tracking
    facts_stored: int = 0
    facts_failed: int = 0
    facts_pending: int = 0
    # Per-sub-batch (source_id, channel_id, message_id) keys — populated as
    # the batch runs so ExtractionWorker can attribute success/failure
    # per sub-batch instead of per-tick. Empty list when the batch is
    # constructed without source messages (e.g. an early failure path).
    # See decision D1 in design.md.
    keys: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class BatchResult:
    """Accumulated result across all processed batches."""

    total_facts: int = 0
    total_entities: int = 0
    total_relationships: int = 0
    batch_breakdowns: list[BatchBreakdown] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    # Weaviate UUIDs for every fact persisted across all batches.
    # Populated on the success path so ExtractionWorker can hand them to
    # WikiMaintainer without a follow-up Weaviate scan.
    fact_ids: list[str] = field(default_factory=list)


# ── Per-sync metrics registry ──────────────────────────────────────────────────
# Process-local counters keyed by (channel_id, sync_job_id).  Initialised at
# sync start, drained + cleared when the 4 sync_summary: lines are emitted.
# No Mongo persistence — these are observational only.

import threading as _threading  # noqa: E402  — intentionally local to section

_sync_metrics_lock = _threading.Lock()
# _sync_metrics[(channel_id, sync_job_id)] -> {"metric_name": int|float}
_sync_metrics: dict[tuple[str, str], dict[str, float]] = {}


def _init_sync_metrics(channel_id: str, sync_job_id: str) -> None:
    """Create a fresh metrics bucket for this sync."""
    key = (channel_id, sync_job_id)
    with _sync_metrics_lock:
        _sync_metrics[key] = {
            "relationships_dropped_total": 0,
            "entity_truncation_recoveries": 0,
            "lost_estimate_sum": 0,
            "cross_batch_validator_llm_fallback_total": 0,
        }


def increment_sync_metric(channel_id: str, sync_job_id: str, metric: str, delta: float = 1) -> None:
    """Atomically increment a per-sync metric counter.

    Called from quality_gates.py, cross_batch_validator.py, and neo4j_store.py.
    Safe to call concurrently from multiple batch coroutines.
    No-ops silently when the key is missing (metric window closed or unknown sync).
    """
    key = (channel_id, sync_job_id)
    with _sync_metrics_lock:
        bucket = _sync_metrics.get(key)
        if bucket is not None:
            bucket[metric] = bucket.get(metric, 0) + delta


def _drain_sync_metrics(channel_id: str, sync_job_id: str) -> dict[str, float]:
    """Return the accumulated metrics dict and remove the key from the registry."""
    key = (channel_id, sync_job_id)
    with _sync_metrics_lock:
        return _sync_metrics.pop(key, {})


if TYPE_CHECKING:
    from beever_atlas.services.circuit_breaker import CircuitBreaker


class BatchProcessor:
    """Chunks messages into batches and runs each through the ingestion pipeline."""

    def __init__(self, breaker: "CircuitBreaker | None" = None) -> None:
        # Inject the CircuitBreaker so test fixtures can hand in a fresh
        # instance and there are no module-globals to bleed across tests.
        # Default singleton is shared with ExtractionWorker so a 503 storm
        # trips one breaker that both call sites observe.
        from beever_atlas.services.circuit_breaker import get_circuit_breaker

        self._breaker = breaker or get_circuit_breaker()

    async def process_messages(
        self,
        messages: list[Any],
        channel_id: str,
        channel_name: str,
        sync_job_id: str,
        ingestion_config: IngestionConfig | None = None,
        use_batch_api: bool = False,
        batch_index_offset: int = 0,
    ) -> BatchResult:
        """Process all messages in fixed-size batches.

        Args:
            messages: List of NormalizedMessage (or dict-serialisable) objects.
            channel_id: Slack/platform channel identifier.
            channel_name: Human-readable channel name.
            sync_job_id: MongoDB SyncJob ID for progress tracking.
            ingestion_config: Per-channel ingestion overrides (optional).
            batch_index_offset: Global batch counter offset. The decoupled
                ExtractionWorker calls process_messages once per tick, and
                each tick claims a slice of the channel's pending rows. With
                offset=0 every tick would restart batch numbering at 1,
                making the activity_log batch_idx values churn for the UI
                (Batch 1 in tick A means different messages than Batch 1
                in tick B). Passing the sync's ``batches_completed`` count
                as the offset shifts this tick's internal batches 1..K to
                global indices ``(offset+1)..(offset+K)``, giving the
                user-facing sync_jobs row a stable 1..ceil(N/batch_size)
                numbering for the whole sync.

        Returns:
            BatchResult with accumulated fact/entity counts and any errors.
        """
        settings = get_settings()
        stores = get_stores()
        result = BatchResult()

        # Initialise per-sync metric counters (process-local, no Mongo).
        # The bucket is drained on the success path below; the caller
        # (ExtractionWorker) wraps process_messages in a try/finally that
        # also drains on exception paths to prevent unbounded dict growth.
        _init_sync_metrics(channel_id, sync_job_id)

        # Use per-channel config if provided, else fall back to global settings
        batch_size = (
            ingestion_config.batch_size
            if ingestion_config and ingestion_config.batch_size is not None
            else settings.sync_batch_size
        )
        total = len(messages)
        # Use token-aware adaptive batching when configured, else fixed-size
        if settings.batch_max_prompt_tokens > 0:
            from beever_atlas.services.adaptive_batcher import token_aware_batches

            # Resolve max_facts_per_message once so batcher's output estimator
            # matches what the extractor is actually told to produce.
            _max_facts_for_batching = (
                ingestion_config.max_facts_per_message
                if ingestion_config and ingestion_config.max_facts_per_message is not None
                else settings.max_facts_per_message
            )
            _output_budget = (
                settings.batch_max_output_tokens if settings.batch_max_output_tokens > 0 else None
            )
            batches = token_aware_batches(
                [m if isinstance(m, dict) else vars(m) for m in messages],
                max_tokens=settings.batch_max_prompt_tokens,
                time_window_seconds=settings.batch_time_window_seconds,
                max_output_tokens=_output_budget,
                max_facts_per_message=_max_facts_for_batching,
                max_messages=settings.batch_max_messages,
            )
        else:
            batches = _thread_aware_batches(messages, batch_size)
        max_batches = len(batches)
        logger.info(
            "BatchProcessor: start job_id=%s channel=%s (%s) total_messages=%d batch_size=%d total_batches=%d",
            sync_job_id,
            channel_id,
            channel_name,
            total,
            batch_size,
            max_batches,
        )

        runner = create_runner(create_ingestion_pipeline())

        # Issue #223 / Layer 1 (ROOT) — make the long extraction call stream.
        # When INGEST_ADK_STREAMING_SSE is on, hand ADK a RunConfig with
        # StreamingMode.SSE so base_llm_flow dispatches the native google-genai
        # generate_content_stream instead of the blocking generate_content. The
        # incremental chunks keep the socket warm so a >120s gemini-2.5-pro call
        # never idles to the edge-proxy disconnect threshold. Built once per job
        # and reused for every sub-batch runner.run_async() below; left None when
        # the flag is off so ADK keeps its current non-streaming default.
        # ``is True`` (not mere truthiness) so an incomplete MagicMock settings
        # in older tests — whose auto-created attribute is truthy but not a real
        # bool — keeps ADK's non-streaming default instead of spuriously
        # streaming. Real Settings stores a genuine ``bool`` so production is
        # unaffected; the streaming test sets the attribute to ``True`` explicitly.
        _ingest_run_config = (
            RunConfig(streaming_mode=StreamingMode.SSE)
            if getattr(settings, "ingest_adk_streaming_sse", False) is True
            else None
        )

        known_entities: list[dict[str, Any]] = await stores.entity_registry.get_all_canonical()
        cumulative_timings: dict[str, float] = {}

        # ── Bounded-concurrency batch execution ───────────────────────────────
        sem = asyncio.Semaphore(settings.ingest_batch_concurrency)

        async def _run_single_batch(
            batch_index: int,
            batch: list[Any],
            known_entities_snapshot: list[dict[str, Any]],
        ) -> tuple[BatchBreakdown, dict[str, float], bool]:
            """Run one batch. Returns (breakdown, stage_timings, entities_were_persisted)."""

            logger.info(
                "BatchProcessor: start batch=%d/%d job_id=%s channel=%s messages=%d",
                batch_index,
                max_batches,
                sync_job_id,
                channel_id,
                len(batch),
            )
            # Phase 0 / Task 1.1 — sub-batch start observability event.
            # No logic change; the event lands in the pipeline_events
            # ring so the API can surface a "recent_events" feed without
            # a new transport.
            logger.info(
                "BatchProcessor: subbatch_event channel=%s batch=%d stage=%s phase=start messages=%d",
                channel_id,
                batch_index,
                "batch",
                len(batch),
            )
            try:
                get_pipeline_events().record(
                    channel_id=channel_id,
                    stage="subbatch",
                    label=f"Batch {batch_index}/{max_batches} started ({len(batch)} messages)",
                )
            except Exception:  # noqa: BLE001 — observability must never break the worker
                logger.debug(
                    "BatchProcessor: pipeline_events.record start failed batch=%d",
                    batch_index,
                    exc_info=True,
                )
            # wiki-redesign-gap-fill / Group 1 — emit message_processing
            # events so SyncMonitor's left pane (Message Stream) shows
            # the messages currently going through ingestion. Best-effort
            # emit; preview is bounded to 200 chars by emit_message_processing.
            from beever_atlas.services.pipeline_events import (
                emit_agent_state as _emit_agent_state,
            )
            from beever_atlas.services.pipeline_events import (
                emit_message_processing as _emit_message_processing,
            )

            _batch_id = f"{sync_job_id}:{batch_index}"
            for _msg in batch:
                try:
                    _msg_id = (
                        getattr(_msg, "message_id", None)
                        or (_msg.get("message_id") if isinstance(_msg, dict) else None)
                        or ""
                    )
                    _msg_text = (
                        getattr(_msg, "content", None)
                        or (_msg.get("content") if isinstance(_msg, dict) else None)
                        or ""
                    )
                    _msg_author = (
                        getattr(_msg, "author", None)
                        or (_msg.get("author") if isinstance(_msg, dict) else None)
                        or ""
                    )
                    _msg_ts = getattr(_msg, "timestamp", None) or (
                        _msg.get("timestamp") if isinstance(_msg, dict) else None
                    )
                    _emit_message_processing(
                        channel_id,
                        message_id=str(_msg_id),
                        text_preview=str(_msg_text),
                        author=str(_msg_author),
                        ts=_msg_ts if hasattr(_msg_ts, "isoformat") else None,
                    )
                except Exception:  # noqa: BLE001
                    pass
            # Emit agent_state(running) for every ingestion agent at
            # batch start so SyncMonitor's Agent Activity pane lights
            # up. Each agent's `done` event is emitted at its output
            # detection site below.
            for _agent in (
                "fact_extractor",
                "entity_extractor",
                "coreference_resolver",
                "embedder",
                "persister",
                "wiki_maintainer",
            ):
                _emit_agent_state(channel_id, _agent, "running", batch_id=_batch_id)
            await stores.mongodb.update_sync_progress(
                job_id=sync_job_id,
                processed=0,
                current_batch=batch_index,
                total_batches=max_batches,
            )
            # Convert NormalizedMessage objects to plain dicts for session state.
            messages_as_dicts: list[dict[str, Any]] = [
                m if isinstance(m, dict) else vars(m) for m in batch
            ]

            if use_batch_api:
                from beever_atlas.services.batch_pipeline import BatchPipelineRunner

                pipeline_runner = BatchPipelineRunner()
                breakdown = await pipeline_runner.process_batch_with_retry(
                    messages=messages_as_dicts,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    sync_job_id=sync_job_id,
                    batch_num=batch_index,
                    max_batches=max_batches,
                    known_entities=known_entities_snapshot,
                    ingestion_config=ingestion_config,
                )
                # Always carry the sub-batch keys so the worker can attribute
                # success/failure per sub-batch even when the batch path runs
                # through the BatchPipelineRunner.
                if not breakdown.keys:
                    breakdown.keys = _keys_for_batch(batch)
                await stores.mongodb.update_sync_progress(
                    job_id=sync_job_id,
                    processed=0,
                    current_batch=batch_index,
                    current_stage=f"Step 7/7 — Batch {batch_index} complete",
                    batch_result=asdict(breakdown),
                )
                return breakdown, {}, False, []

            # Embedding similarity pre-computation is deferred: entity_tags
            # are not available on raw messages before extraction runs.
            embedding_similarity_candidates: list[dict[str, Any]] = []

            _max_facts = (
                ingestion_config.max_facts_per_message
                if ingestion_config and ingestion_config.max_facts_per_message is not None
                else settings.max_facts_per_message
            )
            # Resolve the batch's source language. When detection is enabled,
            # sniff the batch's dominant language so extractor prompts
            # receive a concrete BCP-47 tag via {source_language} and facts/
            # entities can be tagged with `source_lang` at persist time.
            # When disabled, we hardcode "en" so the pipeline behaves
            # byte-identically to the pre-change implementation.
            _batch_source_lang = "en"
            if settings.language_detection_enabled:
                try:
                    from beever_atlas.services.language_detector import (
                        detect_channel_primary_language,
                    )

                    _sample_texts = [
                        str(m.get("text") or m.get("content") or "") for m in messages_as_dicts
                    ]
                    _batch_source_lang, _ = detect_channel_primary_language(
                        _sample_texts,
                        confidence_threshold=settings.language_detection_confidence_threshold,
                        default=settings.default_target_language,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "BatchProcessor: language detection failed, defaulting to en",
                        exc_info=True,
                    )
                    _batch_source_lang = "en"

            initial_state: dict[str, Any] = {
                "messages": messages_as_dicts,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "batch_num": batch_index,
                "max_facts_per_message": _max_facts,
                "known_entities": known_entities_snapshot,
                "embedding_similarity_candidates": embedding_similarity_candidates,
                "sync_job_id": sync_job_id,
                "source_language": _batch_source_lang,
                "skip_entity_extraction": bool(
                    ingestion_config and ingestion_config.skip_entity_extraction
                ),
                "skip_graph_writes": bool(ingestion_config and ingestion_config.skip_graph_writes),
                "quality_threshold": (
                    ingestion_config.quality_threshold
                    if ingestion_config and ingestion_config.quality_threshold is not None
                    else None
                ),
            }

            # Load checkpoint if this batch was partially processed before
            checkpoint = await stores.mongodb.load_pipeline_checkpoint(
                sync_job_id=sync_job_id,
                batch_num=batch_index,
            )
            _resumed_from: str | None = None
            _skipped_stage_count = 0
            if checkpoint:
                _resumed_from = checkpoint["completed_stage"]
                _skipped_stage_count = checkpoint["completed_stage_index"] + 1
                snapshot = checkpoint.get("state_snapshot") or {}
                for key in _ALL_CHECKPOINT_KEYS:
                    if key in snapshot:
                        initial_state[key] = snapshot[key]
                logger.info(
                    "BatchProcessor: resuming from checkpoint job_id=%s batch=%d/%d "
                    "last_completed=%s skipping=%d stages",
                    sync_job_id,
                    batch_index,
                    max_batches,
                    _resumed_from,
                    _skipped_stage_count,
                )

            session = await create_session(
                user_id="system",
                state=initial_state,
            )

            # Drive the pipeline to completion with retry on transient LLM errors.
            # Each attempt gets its own fresh timeout budget so retry sleeps
            # don't consume pipeline time.
            batch_stage_timings: dict[str, float] = {}
            for attempt in range(_LLM_MAX_RETRIES + 1):
                try:
                    # Each retry needs a fresh session since the pipeline
                    # may have partially mutated the previous one.
                    if attempt > 0:
                        # Sleep between retries OUTSIDE the timeout scope
                        base = _LLM_RETRY_BACKOFF[attempt - 1]
                        jittered = base * (1 + random.uniform(-0.25, 0.25))
                        logger.warning(
                            "BatchProcessor: retrying job_id=%s batch=%d/%d "
                            "attempt=%d/%d after %ds sleep",
                            sync_job_id,
                            batch_index,
                            max_batches,
                            attempt + 1,
                            _LLM_MAX_RETRIES + 1,
                            base,
                        )
                        await stores.mongodb.update_batch_stage(
                            job_id=sync_job_id,
                            batch_idx=batch_index,
                            label=f"Step 0/6 — Retrying in {base}s (attempt {attempt + 1}/{_LLM_MAX_RETRIES + 1})",
                        )
                        await asyncio.sleep(jittered)
                        # Phase 1 Step 2 (ingestion-pipeline-hardening): unconditionally
                        # re-consult the checkpoint store before every retry, regardless of
                        # which exception class triggered the retry. Without this, an
                        # httpx.HTTPStatusError from the embedder could restart from Stage 1
                        # and re-run expensive LLM fact/entity extraction that was already
                        # checkpointed. Retry count is the only gate.
                        _retry_checkpoint = await stores.mongodb.load_pipeline_checkpoint(
                            sync_job_id=sync_job_id,
                            batch_num=batch_index,
                        )
                        if _retry_checkpoint:
                            _resumed_from = _retry_checkpoint["completed_stage"]
                            _skipped_stage_count = _retry_checkpoint["completed_stage_index"] + 1
                            _retry_snapshot = _retry_checkpoint.get("state_snapshot") or {}
                            for _key in _ALL_CHECKPOINT_KEYS:
                                if _key in _retry_snapshot:
                                    initial_state[_key] = _retry_snapshot[_key]
                            logger.info(
                                "BatchProcessor: retry resuming from checkpoint job_id=%s batch=%d/%d "
                                "attempt=%d last_completed=%s skipping=%d stages",
                                sync_job_id,
                                batch_index,
                                max_batches,
                                attempt + 1,
                                _resumed_from,
                                _skipped_stage_count,
                            )
                        session = await create_session(
                            user_id="system",
                            state=initial_state,
                        )
                    batch_stage_timings = {}
                    activity_log: list[dict[str, Any]] = []

                    async def _push_activity(entry: dict[str, Any]) -> None:
                        """Append locally and atomically push to MongoDB so the UI feed updates live."""
                        activity_log.append(entry)
                        try:
                            await stores.mongodb.push_activity_log_entry(
                                job_id=sync_job_id,
                                batch_idx=batch_index,
                                entry=entry,
                            )
                        except Exception as exc:
                            logger.warning(
                                "push_activity_log_entry failed job_id=%s batch=%d: %s",
                                sync_job_id,
                                batch_index,
                                exc,
                            )

                    # Acquired per-attempt around the LLM dispatch and the
                    # immediately surrounding bookkeeping (breaker check,
                    # stage writes, pipeline runner). Released across retry
                    # sleeps so sibling sub-batches can claim the slot
                    # during backoff.
                    _sem_wait_start = time.monotonic()
                    async with sem:
                        _semaphore_wait_s = time.monotonic() - _sem_wait_start
                        _semaphore_waits.append(_semaphore_wait_s)
                        _batch_idx_var.set(batch_index)
                        logger.debug(
                            "BatchProcessor: semaphore_acquired batch=%d job_id=%s wait_s=%.3f",
                            batch_index,
                            sync_job_id,
                            _semaphore_wait_s,
                        )
                        # ── Circuit breaker: fail fast if provider is down ────────────
                        # The injected breaker replaces the old module-globals.
                        # The half-open recovery path is automatic — if the breaker is
                        # open but the cooldown has elapsed, allow() transitions to
                        # half_open and returns True, letting one probe through.
                        if not await self._breaker.allow():
                            snapshot = self._breaker.snapshot()
                            logger.error(
                                "BatchProcessor: provider outage breaker tripped "
                                "consecutive=%d threshold=%d state=%s",
                                snapshot.consecutive_failures,
                                snapshot.threshold,
                                snapshot.state,
                            )
                            raise ProviderOutageError(
                                f"Provider outage: {snapshot.consecutive_failures} "
                                f"consecutive Gemini 5xx failures"
                            )
                        # ─────────────────────────────────────────────────────────────
                        _logged_outputs: set[str] = (
                            set()
                        )  # Track which state keys we already logged
                        _last_stage = ""
                        _stage_start = time.monotonic()
                        _batch_wall_start = time.monotonic()
                        _limiter_wait_gemini = 0.0
                        _limiter_wait_embedding = 0.0
                        _evt_count = 0
                        # Issue #223 / Layer 1 — only pass run_config when SSE
                        # streaming is enabled so ADK keeps its own default
                        # RunConfig (non-streaming) when the flag is off.
                        _run_async_kwargs: dict[str, Any] = {
                            "user_id": "system",
                            "session_id": session.id,
                            "new_message": types.Content(
                                role="user",
                                parts=[types.Part(text="process batch")],
                            ),
                        }
                        if _ingest_run_config is not None:
                            _run_async_kwargs["run_config"] = _ingest_run_config
                        async for _event in runner.run_async(**_run_async_kwargs):
                            author = getattr(_event, "author", "") or ""
                            label = _STAGE_LABELS.get(author)
                            if label and author != _last_stage:
                                if _last_stage:
                                    batch_stage_timings[_last_stage] = round(
                                        time.monotonic() - _stage_start, 2
                                    )
                                # B2: acquire per-provider rate limiter before the stage runs.
                                # Embedder = Jina; all other LLM stages = Gemini.
                                # preprocessor/persister are local-only, no quota needed.
                                #
                                # memory-then-wiki-pipeline-realignment G6 audit:
                                # The ADK Workflow's parallel fan-out (fact_extractor +
                                # entity_extractor after the preprocessor; embedder +
                                # validator after join_extract) does NOT emit a single
                                # event under a container name — each sub-agent node emits
                                # its own event with ``author = sub_agent.name``. The dispatch
                                # below therefore acquires ONE token per concurrent Gemini
                                # call. No double-counting fix needed; the previous design
                                # hypothesis about under-counting was incorrect.
                                if author == "embedder":
                                    _lim_t0 = time.monotonic()
                                    await (await _get_limiter("embedding")).acquire()
                                    _limiter_wait_embedding += time.monotonic() - _lim_t0
                                elif author not in ("preprocessor", "persister"):
                                    _lim_t0 = time.monotonic()
                                    await (await _get_limiter("gemini")).acquire()
                                    _limiter_wait_gemini += time.monotonic() - _lim_t0
                                _last_stage = author
                                _stage_start = time.monotonic()
                                _provider = get_llm_provider()
                                if author == "embedder":
                                    _stage_model = _provider.embedding_model
                                elif author in ("preprocessor", "persister"):
                                    _stage_model = None
                                else:
                                    _stage_model = _provider.get_model_string(author)
                                await _push_activity(
                                    {
                                        "agent": author,
                                        "stage": label,
                                        "type": "stage_start",
                                        "model": _stage_model,
                                    }
                                )

                                # Save checkpoint on stage transitions
                                if _last_stage and _last_stage != "persister":
                                    try:
                                        from beever_atlas.agents.runner import get_session_service

                                        _cp_svc = get_session_service()
                                        _cp_session = await _cp_svc.get_session(
                                            app_name="beever_atlas",
                                            user_id="system",
                                            session_id=session.id,
                                        )
                                        _cp_state = _cp_session.state if _cp_session else {}
                                        _cp_snapshot = {
                                            k: _cp_state[k]
                                            for k in _ALL_CHECKPOINT_KEYS
                                            if k in _cp_state
                                        }
                                        _cp_idx = (
                                            _STAGE_ORDER.index(_last_stage)
                                            if _last_stage in _STAGE_ORDER
                                            else -1
                                        )
                                        await stores.mongodb.save_pipeline_checkpoint(
                                            sync_job_id=sync_job_id,
                                            batch_num=batch_index,
                                            channel_id=channel_id,
                                            completed_stage=_last_stage,
                                            completed_stage_index=_cp_idx,
                                            state_snapshot=_cp_snapshot,
                                            stage_timings=batch_stage_timings,
                                        )
                                    except Exception:
                                        logger.warning(
                                            "BatchProcessor: checkpoint save failed job_id=%s batch=%d stage=%s",
                                            sync_job_id,
                                            batch_index,
                                            _last_stage,
                                            exc_info=True,
                                        )

                            # Extract meaningful content from state_delta events.
                            # Only capture non-empty outputs to avoid duplicate/intermediate entries.
                            actions = getattr(_event, "actions", None)
                            if actions:
                                delta = (
                                    getattr(actions, "state_delta", None)
                                    or getattr(actions, "stateDelta", None)
                                    or {}
                                )
                                if isinstance(delta, dict):
                                    # ── Preprocessor output ────────────────────
                                    if (
                                        "preprocessed_messages" in delta
                                        and "preprocessed_messages" not in _logged_outputs
                                    ):
                                        msgs = delta["preprocessed_messages"]
                                        if isinstance(msgs, list) and msgs:
                                            _logged_outputs.add("preprocessed_messages")
                                            import re as _re

                                            msg_details: list[str] = []
                                            media_count = 0
                                            coref_count = 0
                                            thread_count = 0
                                            link_count = 0
                                            for m in msgs:
                                                author = (
                                                    m.get("author_name") or m.get("username") or "?"
                                                )
                                                full_text = m.get("text") or ""
                                                first_line = full_text.split("\n")[0][:200]
                                                badges: list[str] = []
                                                if (
                                                    m.get("raw_text")
                                                    and m.get("raw_text") != full_text
                                                ):
                                                    badges.append("COREF")
                                                    coref_count += 1
                                                if m.get("thread_context"):
                                                    badges.append("THREAD")
                                                    thread_count += 1
                                                if m.get("source_link_urls"):
                                                    badges.append(
                                                        f"LINKS:{len(m['source_link_urls'])}"
                                                    )
                                                    link_count += 1
                                                if m.get("modality") == "mixed":
                                                    mtype = m.get("source_media_type", "")
                                                    media_icons = {
                                                        "image": "🖼",
                                                        "pdf": "📄",
                                                        "video": "🎬",
                                                        "audio": "🎵",
                                                    }
                                                    icon = media_icons.get(mtype, "📎")
                                                    badges.append(
                                                        f"{icon} {mtype.upper()}"
                                                        if mtype
                                                        else "📎 MEDIA"
                                                    )
                                                    media_count += 1
                                                badge_str = (
                                                    f" [{', '.join(badges)}]" if badges else ""
                                                )
                                                msg_details.append(
                                                    f"{author}: {first_line}{'…' if len(full_text.split(chr(10))[0]) > 200 else ''}{badge_str}"
                                                )

                                                # Extract media observation details into structured samples
                                                # Fix: Use re.DOTALL and properly match brackets to catch [Document Digest]:
                                                doc_match = _re.search(
                                                    r"\[Document (?:Digest|text)\]:?\s*(.+)",
                                                    full_text,
                                                    _re.DOTALL,
                                                )
                                                if doc_match:
                                                    content_snippet = doc_match.group(1).strip()[
                                                        :2000
                                                    ]
                                                    msg_details.append(
                                                        {
                                                            "item_type": "media",
                                                            "agent": "document_digester",
                                                            "content": f"{content_snippet}…",
                                                            "model": get_llm_provider().get_model_string(
                                                                "document_digester"
                                                            ),
                                                        }
                                                    )

                                                img_match = _re.search(
                                                    r"\[Image description\]:?\s*(.+)",
                                                    full_text,
                                                    _re.DOTALL,
                                                )
                                                if img_match:
                                                    content_snippet = (
                                                        img_match.group(1)
                                                        .strip()
                                                        .split("\n")[0][:500]
                                                    )
                                                    msg_details.append(
                                                        {
                                                            "item_type": "media",
                                                            "agent": "image_describer",
                                                            "content": f"{content_snippet}…",
                                                            "model": get_llm_provider().get_model_string(
                                                                "image_describer"
                                                            ),
                                                        }
                                                    )
                                                if not img_match:
                                                    img_meta = _re.search(
                                                        r"\[Attachment:.*?\(image", full_text
                                                    )
                                                    if img_meta:
                                                        msg_details.append(
                                                            {
                                                                "item_type": "media",
                                                                "agent": "image_describer",
                                                                "content": "Vision skipped (message text sufficient)",
                                                                "model": get_llm_provider().get_model_string(
                                                                    "image_describer"
                                                                ),
                                                                "status": "skipped",
                                                            }
                                                        )

                                                vid_match = _re.search(
                                                    r"\[Video (?:summary|transcript|analysis)\]:?\s*(.+)",
                                                    full_text,
                                                    _re.DOTALL,
                                                )
                                                if vid_match:
                                                    content_snippet = vid_match.group(1).strip()[
                                                        :2000
                                                    ]
                                                    msg_details.append(
                                                        {
                                                            "item_type": "media",
                                                            "agent": "video_describer",
                                                            "content": f"{content_snippet}…",
                                                            "model": get_llm_provider().get_model_string(
                                                                "video_analyzer"
                                                            ),
                                                        }
                                                    )

                                                vid_vis = _re.search(
                                                    r"\[Video visual description\]:?\s*(.+)",
                                                    full_text,
                                                    _re.DOTALL,
                                                )
                                                if vid_vis:
                                                    content_snippet = vid_vis.group(1).strip()[
                                                        :2000
                                                    ]
                                                    msg_details.append(
                                                        {
                                                            "item_type": "media",
                                                            "agent": "video_describer",
                                                            "content": f"{content_snippet}…",
                                                            "model": get_llm_provider().get_model_string(
                                                                "video_analyzer"
                                                            ),
                                                        }
                                                    )

                                                aud_match = _re.search(
                                                    r"\[Audio (?:summary|transcript)\]:?\s*(.+)",
                                                    full_text,
                                                    _re.DOTALL,
                                                )
                                                if aud_match:
                                                    content_snippet = aud_match.group(1).strip()[
                                                        :2000
                                                    ]
                                                    msg_details.append(
                                                        {
                                                            "item_type": "media",
                                                            "agent": "audio_describer",
                                                            "content": f"{content_snippet}…",
                                                            "model": get_llm_provider().get_model_string(
                                                                "audio_transcriber"
                                                            ),
                                                        }
                                                    )

                                                # Detect failed media agents (timeout or other errors)
                                                if m.get("modality") == "mixed":
                                                    img_meta = _re.search(
                                                        r"\[Attachment:.*?\(image", full_text
                                                    )
                                                    has_media_output = bool(
                                                        doc_match
                                                        or img_match
                                                        or img_meta
                                                        or vid_match
                                                        or vid_vis
                                                        or aud_match
                                                    )
                                                    if not has_media_output:
                                                        media_type = m.get("source_media_type", "")
                                                        agent_map = {
                                                            "pdf": "document_digester",
                                                            "image": "image_describer",
                                                            "video": "video_analyzer",
                                                            "audio": "audio_transcriber",
                                                        }
                                                        model_map = {
                                                            "pdf": "document_digester",
                                                            "image": "image_describer",
                                                            "video": "video_analyzer",
                                                            "audio": "audio_transcriber",
                                                        }
                                                        agent = agent_map.get(
                                                            media_type, "media_processor"
                                                        )
                                                        model_key = model_map.get(
                                                            media_type, "document_digester"
                                                        )
                                                        media_names = m.get(
                                                            "source_media_names", []
                                                        )
                                                        file_name = (
                                                            media_names[0]
                                                            if media_names
                                                            else "unknown"
                                                        )
                                                        is_timeout = (
                                                            "processing timed out"
                                                            in full_text.lower()
                                                        )
                                                        msg_details.append(
                                                            {
                                                                "item_type": "media",
                                                                "agent": agent,
                                                                "content": f"Processing timed out for {file_name}"
                                                                if is_timeout
                                                                else f"Processing failed for {file_name}",
                                                                "model": get_llm_provider().get_model_string(
                                                                    model_key
                                                                ),
                                                                "status": "timeout"
                                                                if is_timeout
                                                                else "error",
                                                            }
                                                        )

                                                msg_details.append(
                                                    {
                                                        "item_type": "message",
                                                        "author": author,
                                                        "content": f"{first_line}{'…' if len(full_text.split(chr(10))[0]) > 200 else ''}",
                                                        "tags": badges,
                                                    }
                                                )

                                            summary_parts = [f"Retained {len(msgs)} messages"]
                                            if media_count:
                                                summary_parts.append(f"{media_count} media")
                                            if coref_count:
                                                summary_parts.append(f"{coref_count} coref")
                                            if thread_count:
                                                summary_parts.append(f"{thread_count} threads")
                                            if link_count:
                                                summary_parts.append(f"{link_count} links")
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            await _push_activity(
                                                {
                                                    "type": "stage_output",
                                                    "agent": "preprocessor",
                                                    "message": " · ".join(summary_parts),
                                                    "metrics": {
                                                        "messages": len(msgs),
                                                        "media": media_count,
                                                    },
                                                    "samples": msg_details[:20],
                                                    "elapsed": elapsed,
                                                }
                                            )
                                            # Phase 0 / Task 1.3 — pipeline event hook
                                            try:
                                                get_pipeline_events().record(
                                                    channel_id=channel_id,
                                                    stage="preprocess",
                                                    label=" · ".join(summary_parts),
                                                )
                                            except Exception:  # noqa: BLE001
                                                pass

                                    # ── Fact extraction output ─────────────────
                                    if (
                                        "extracted_facts" in delta
                                        and "extracted_facts" not in _logged_outputs
                                    ):
                                        raw = delta["extracted_facts"]
                                        facts_list = (
                                            raw.get("facts", [])
                                            if isinstance(raw, dict)
                                            else (raw if isinstance(raw, list) else [])
                                        )
                                        if facts_list:
                                            _logged_outputs.add("extracted_facts")
                                            fact_summaries = []
                                            for f in facts_list[:5]:
                                                text = (f.get("memory_text") or "")[:300]
                                                score = f.get("quality_score", 0)
                                                imp = f.get("importance", "?")
                                                fact_summaries.append(
                                                    {
                                                        "item_type": "fact",
                                                        "content": text,
                                                        "score": score,
                                                        "tags": [imp],
                                                    }
                                                )
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            avg_quality = (
                                                sum(f.get("quality_score", 0) for f in facts_list)
                                                / len(facts_list)
                                                if facts_list
                                                else 0
                                            )
                                            await _push_activity(
                                                {
                                                    "type": "stage_output",
                                                    "agent": "fact_extractor",
                                                    "message": f"Extracted {len(facts_list)} facts (avg quality {avg_quality:.2f})",
                                                    "model": get_llm_provider().get_model_string(
                                                        "fact_extractor"
                                                    ),
                                                    "metrics": {
                                                        "count": len(facts_list),
                                                        "avg_quality": float(f"{avg_quality:.2f}"),
                                                    },
                                                    "samples": fact_summaries,
                                                    "elapsed": elapsed,
                                                }
                                            )
                                            # Phase 0 / Task 1.3 — pipeline event hook
                                            try:
                                                get_pipeline_events().record(
                                                    channel_id=channel_id,
                                                    stage="extract",
                                                    label=(
                                                        f"Extracted {len(facts_list)} facts "
                                                        f"(avg quality {avg_quality:.2f})"
                                                    ),
                                                )
                                            except Exception:  # noqa: BLE001
                                                pass
                                            # Group 1 — agent_state(done)
                                            _emit_agent_state(
                                                channel_id,
                                                "fact_extractor",
                                                "done",
                                                batch_id=_batch_id,
                                                elapsed_ms=int(elapsed * 1000),
                                            )

                                    # ── Entity extraction output ───────────────
                                    if (
                                        "extracted_entities" in delta
                                        and "extracted_entities" not in _logged_outputs
                                    ):
                                        raw = delta["extracted_entities"]
                                        entities = (
                                            raw.get("entities", []) if isinstance(raw, dict) else []
                                        )
                                        rels = (
                                            raw.get("relationships", [])
                                            if isinstance(raw, dict)
                                            else []
                                        )
                                        if entities or rels:
                                            _logged_outputs.add("extracted_entities")
                                            entity_details = [
                                                {
                                                    "item_type": "entity",
                                                    "content": e.get("name", "?"),
                                                    "tags": [e.get("type", "?")],
                                                }
                                                for e in entities[:8]
                                            ]
                                            rel_details = [
                                                {
                                                    "item_type": "relationship",
                                                    "source": r.get("source", "?"),
                                                    "rel_type": r.get("type", "?"),
                                                    "target": r.get("target", "?"),
                                                }
                                                for r in rels[:5]
                                            ]
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            await _push_activity(
                                                {
                                                    "type": "stage_output",
                                                    "agent": "entity_extractor",
                                                    "message": f"Found {len(entities)} entities, {len(rels)} relationships",
                                                    "model": get_llm_provider().get_model_string(
                                                        "entity_extractor"
                                                    ),
                                                    "metrics": {
                                                        "entities": len(entities),
                                                        "relationships": len(rels),
                                                    },
                                                    "samples": entity_details + rel_details,
                                                    "elapsed": elapsed,
                                                }
                                            )
                                            # Group 1 — agent_state(done) for
                                            # entity_extractor + coreference
                                            # resolver (the latter runs as part
                                            # of entity extraction).
                                            _emit_agent_state(
                                                channel_id,
                                                "entity_extractor",
                                                "done",
                                                batch_id=_batch_id,
                                                elapsed_ms=int(elapsed * 1000),
                                            )
                                            _emit_agent_state(
                                                channel_id,
                                                "coreference_resolver",
                                                "done",
                                                batch_id=_batch_id,
                                                elapsed_ms=int(elapsed * 1000),
                                            )

                                    # ── Embedder output ────────────────────────
                                    if (
                                        "embedded_facts" in delta
                                        and "embedded_facts" not in _logged_outputs
                                    ):
                                        embedded = delta["embedded_facts"]
                                        count = len(embedded) if isinstance(embedded, list) else 0
                                        if count > 0:
                                            _logged_outputs.add("embedded_facts")
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            await _push_activity(
                                                {
                                                    "type": "stage_output",
                                                    "agent": "embedder",
                                                    "message": f"Embedded {count} facts",
                                                    "model": get_llm_provider().embedding_model,
                                                    "metrics": {"embedded": count},
                                                    "elapsed": elapsed,
                                                }
                                            )
                                            # Phase 0 / Task 1.3 — pipeline event hook
                                            try:
                                                get_pipeline_events().record(
                                                    channel_id=channel_id,
                                                    stage="embed",
                                                    label=f"Embedded {count} facts",
                                                )
                                            except Exception:  # noqa: BLE001
                                                pass
                                            # Group 1 — agent_state(done)
                                            _emit_agent_state(
                                                channel_id,
                                                "embedder",
                                                "done",
                                                batch_id=_batch_id,
                                                elapsed_ms=int(elapsed * 1000),
                                            )

                                    # ── Validator output ───────────────────────
                                    if (
                                        "validated_entities" in delta
                                        and "validated_entities" not in _logged_outputs
                                    ):
                                        raw = delta["validated_entities"]
                                        entities = (
                                            raw.get("entities", []) if isinstance(raw, dict) else []
                                        )
                                        merges = (
                                            raw.get("merges", []) if isinstance(raw, dict) else []
                                        )
                                        if entities or merges:
                                            _logged_outputs.add("validated_entities")
                                            merge_details = [
                                                {
                                                    "item_type": "validation",
                                                    "content": f"{', '.join(mg['merged_from']) if isinstance(mg.get('merged_from'), list) else mg.get('merged_from', '?')} → {mg.get('canonical', '?')}",
                                                }
                                                for mg in merges[:5]
                                            ]
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            await _push_activity(
                                                {
                                                    "type": "stage_output",
                                                    "agent": "cross_batch_validator_agent",
                                                    "message": f"Validated {len(entities)} entities"
                                                    + (f", {len(merges)} merges" if merges else ""),
                                                    "model": get_llm_provider().get_model_string(
                                                        "cross_batch_validator_agent"
                                                    ),
                                                    "metrics": {
                                                        "entities": len(entities),
                                                        "merges": len(merges),
                                                    },
                                                    "samples": merge_details
                                                    if merge_details
                                                    else None,
                                                    "elapsed": elapsed,
                                                }
                                            )

                                    # ── Persister output ───────────────────────
                                    if (
                                        "persist_result" in delta
                                        and "persist_result" not in _logged_outputs
                                    ):
                                        pr = delta["persist_result"]
                                        wv_count = len(pr.get("weaviate_ids", []))
                                        neo_count = pr.get("entity_count", 0)
                                        rel_count = pr.get("relationship_count", 0)
                                        if wv_count > 0 or neo_count > 0:
                                            _logged_outputs.add("persist_result")
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            await _push_activity(
                                                {
                                                    "type": "stage_output",
                                                    "agent": "persister",
                                                    "message": f"Saved {wv_count} facts → Weaviate, {neo_count} entities + {rel_count} rels → Neo4j",
                                                    "metrics": {
                                                        "weaviate_facts": wv_count,
                                                        "neo4j_entities": neo_count,
                                                        "neo4j_rels": rel_count,
                                                    },
                                                    "elapsed": elapsed,
                                                }
                                            )
                                            # Group 1 — agent_state(done)
                                            _emit_agent_state(
                                                channel_id,
                                                "persister",
                                                "done",
                                                batch_id=_batch_id,
                                                elapsed_ms=int(elapsed * 1000),
                                            )
                                            # Phase 0 / Task 1.3 — pipeline event hook
                                            try:
                                                get_pipeline_events().record(
                                                    channel_id=channel_id,
                                                    stage="persist",
                                                    label=(
                                                        f"Saved {wv_count} facts, "
                                                        f"{neo_count} entities, {rel_count} rels"
                                                    ),
                                                )
                                            except Exception:  # noqa: BLE001
                                                pass

                            # Throttle MongoDB updates — only write on stage changes or every 5 events
                            _evt_count += 1
                            if label:
                                # Per-batch atomic dot-path update — race-safe under concurrency.
                                await stores.mongodb.update_batch_stage(
                                    job_id=sync_job_id,
                                    batch_idx=batch_index,
                                    label=label,
                                )
                            elif _evt_count % 5 == 0:
                                # Throttled timing flush without stage label change
                                await stores.mongodb.update_sync_progress(
                                    job_id=sync_job_id,
                                    processed=0,
                                    current_batch=batch_index,
                                    stage_timings=batch_stage_timings,
                                )

                        if _last_stage:
                            batch_stage_timings[_last_stage] = round(
                                time.monotonic() - _stage_start, 2
                            )
                        # D2: extended timing telemetry — batch wall-clock and per-provider
                        # limiter wait accumulated across all stage transitions in this batch.
                        batch_stage_timings["batch_wall_clock_s"] = round(
                            time.monotonic() - _batch_wall_start, 2
                        )
                        if _limiter_wait_gemini > 0:
                            batch_stage_timings["limiter_wait_s_gemini"] = round(
                                _limiter_wait_gemini, 3
                            )
                        if _limiter_wait_embedding > 0:
                            batch_stage_timings["limiter_wait_s_embedding"] = round(
                                _limiter_wait_embedding, 3
                            )
                        logger.debug(
                            "BatchProcessor: D2 timing batch=%d job_id=%s wall=%.2fs "
                            "limiter_gemini=%.3fs limiter_embedding=%.3fs",
                            batch_index,
                            sync_job_id,
                            batch_stage_timings["batch_wall_clock_s"],
                            _limiter_wait_gemini,
                            _limiter_wait_embedding,
                        )
                        # Final progress flush after pipeline completes
                        await stores.mongodb.update_batch_stage(
                            job_id=sync_job_id,
                            batch_idx=batch_index,
                            label=f"Step 7/7 — Batch {batch_index} complete",
                        )
                        await stores.mongodb.update_sync_progress(
                            job_id=sync_job_id,
                            processed=0,
                            current_batch=batch_index,
                            stage_timings=batch_stage_timings,
                        )
                        # Clean up checkpoint after successful completion
                        try:
                            await stores.mongodb.delete_pipeline_checkpoint(
                                sync_job_id=sync_job_id,
                                batch_num=batch_index,
                            )
                        except Exception as exc:
                            logger.debug(
                                "BatchProcessor: checkpoint delete failed job_id=%s batch=%d: %s",
                                sync_job_id,
                                batch_index,
                                exc,
                                exc_info=False,
                            )
                        # Reset breaker on any successful batch
                        await self._breaker.record_success()
                        break  # success
                except (
                    ServerError,
                    httpx.HTTPStatusError,
                    PydanticValidationError,
                    json.JSONDecodeError,
                ) as exc:
                    # A4: broaden checkpoint-aware retry to cover ValidationError and
                    # JSONDecodeError in addition to provider 5xx. _is_resumable gates
                    # which sub-types actually retry (e.g. httpx 4xx still re-raises).
                    if not _is_resumable(exc):
                        raise
                    if attempt < _LLM_MAX_RETRIES:
                        logger.warning(
                            "BatchProcessor: transient error job_id=%s batch=%d/%d "
                            "attempt=%d/%d: %s",
                            sync_job_id,
                            batch_index,
                            max_batches,
                            attempt + 1,
                            _LLM_MAX_RETRIES + 1,
                            exc,
                        )
                        # Sleep and retry happen at the top of the next loop iteration
                    else:
                        # Terminal failure after all retries — increment breaker counter once
                        await self._breaker.record_failure(exc)
                        raise
                except Exception as exc:
                    # Issue #223 / Layer 2 (ROBUSTNESS) — transient connection
                    # drops MUST be handled BEFORE the truncation ladder and MUST
                    # NOT feed the breaker. A long extraction call whose idle
                    # socket is closed by an edge proxy raises
                    # aiohttp.ServerDisconnectedError, which is neither a
                    # resumable type nor a truncation marker, so it would
                    # otherwise hit the terminal ``raise`` below on attempt 0 with
                    # zero backoff — and concurrent batches' drops would open the
                    # breaker and fast-fail the rest of the job into total_facts=0.
                    if _is_transient_net_error(exc):
                        if attempt < _LLM_MAX_RETRIES:
                            logger.warning(
                                "BatchProcessor: transient network drop job_id=%s "
                                "batch=%d/%d attempt=%d/%d — retrying with backoff "
                                "(not counted toward outage breaker): %s",
                                sync_job_id,
                                batch_index,
                                max_batches,
                                attempt + 1,
                                _LLM_MAX_RETRIES + 1,
                                _summarize_exception(exc),
                            )
                            # Loop top sleeps (backoff ladder) and reloads the
                            # checkpoint, so the streamed retry resumes cheaply.
                            continue
                        # Terminal: exhausted retries on a transient drop. Raise so
                        # THIS row fails, but DO NOT call record_failure — an
                        # idle-proxy disconnect storm must never advance the
                        # breaker toward its threshold and wipe the whole batch.
                        logger.error(
                            "BatchProcessor: transient network drop job_id=%s "
                            "batch=%d/%d — exhausted %d retries; failing this row "
                            "WITHOUT tripping the outage breaker: %s",
                            sync_job_id,
                            batch_index,
                            max_batches,
                            _LLM_MAX_RETRIES + 1,
                            _summarize_exception(exc),
                        )
                        raise
                    # Catch ValidationError (truncated LLM JSON) and similar parse failures.
                    # Strategy: attempt 1 → reduce max_facts to 1, attempt 2 → halve batch.
                    is_validation = _is_truncation_error(exc)
                    if is_validation and attempt < _LLM_MAX_RETRIES:
                        current_max = initial_state.get("max_facts_per_message", 2)
                        current_msgs = initial_state.get("messages", [])
                        if current_max > 1:
                            # First: reduce facts per message
                            initial_state["max_facts_per_message"] = 1
                            logger.warning(
                                "BatchProcessor: LLM output truncated job_id=%s batch=%d/%d "
                                "attempt=%d/%d — reducing max_facts to 1 (%d messages): %s",
                                sync_job_id,
                                batch_index,
                                max_batches,
                                attempt + 1,
                                _LLM_MAX_RETRIES + 1,
                                len(current_msgs),
                                str(exc)[:200],
                            )
                        elif len(current_msgs) > 5:
                            # Second: halve the batch (remaining messages will be missed
                            # but the batch won't crash — user can re-sync to catch them)
                            half = len(current_msgs) // 2
                            initial_state["messages"] = current_msgs[:half]
                            logger.warning(
                                "BatchProcessor: LLM still truncating job_id=%s batch=%d/%d "
                                "attempt=%d/%d — halving batch from %d to %d messages: %s",
                                sync_job_id,
                                batch_index,
                                max_batches,
                                attempt + 1,
                                _LLM_MAX_RETRIES + 1,
                                len(current_msgs),
                                half,
                                str(exc)[:200],
                            )
                        else:
                            raise  # Batch is tiny and still truncating — give up
                    else:
                        raise

            # Re-fetch session to read final state written by PersisterAgent.
            from beever_atlas.agents.runner import get_session_service

            session_service = get_session_service()
            final_session = await session_service.get_session(
                app_name="beever_atlas",
                user_id="system",
                session_id=session.id,
            )
            final_state: dict[str, Any] = final_session.state if final_session else {}
            persist_result: dict[str, Any] = final_state.get("persist_result") or {}
            if not persist_result:
                logger.warning(
                    "BatchProcessor: empty persist_result job_id=%s channel=%s batch=%d/%d",
                    sync_job_id,
                    channel_id,
                    batch_index,
                    max_batches,
                )

            batch_facts = len(persist_result.get("weaviate_ids") or [])
            batch_entities = persist_result.get("entity_count") or 0

            # --- Post-pipeline: contradiction detection ---
            # P0-1 (pipeline-cost-latency-reduction-v2): per-batch
            # contradiction firing has been replaced with a single
            # post-sync bulk pass driven by ``memory_settled`` (see
            # ``server/app.py`` subscriber + ``contradiction_detector.
            # check_and_supersede_for_channel``). The legacy per-batch
            # path remains available as the ``defer_contradiction=False``
            # kill switch — when off, we still accumulate ``persisted_facts``
            # below and fire the detached check as before.
            from beever_atlas.infra.config import get_settings as _get_settings

            _defer = bool(getattr(_get_settings(), "defer_contradiction", True))

            persisted_facts: list[Any] = []
            try:
                embedded_facts_raw = final_state.get("embedded_facts") or []
                if embedded_facts_raw:
                    from beever_atlas.models import AtomicFact

                    weaviate_ids = persist_result.get("weaviate_ids") or []
                    for idx, fd in enumerate(embedded_facts_raw):
                        fact_channel = fd.get("channel_id") or channel_id
                        # Content-derived deterministic ID for the
                        # contradiction-detector fallback path. Mirrors the
                        # persister so re-runs map to the same fact_id.
                        entity_names = fd.get("entity_tags") or []
                        fact_id = (
                            weaviate_ids[idx]
                            if idx < len(weaviate_ids)
                            else AtomicFact.deterministic_id(
                                fd.get("memory_text", ""), entity_names
                            )
                        )
                        persisted_facts.append(
                            AtomicFact(
                                id=fact_id,
                                memory_text=fd.get("memory_text", ""),
                                topic_tags=fd.get("topic_tags") or [],
                                entity_tags=fd.get("entity_tags") or [],
                                channel_id=fact_channel,
                            )
                        )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "BatchProcessor: persisted_facts accumulation failed job_id=%s batch=%d, continuing",
                    sync_job_id,
                    batch_index,
                    exc_info=True,
                )

            if _defer:
                logger.debug(
                    "BatchProcessor: contradiction detection deferred to post-sync "
                    "job_id=%s batch=%d facts=%d",
                    sync_job_id,
                    batch_index,
                    len(persisted_facts),
                )
            else:
                # Legacy kill-switch path — fire-and-forget per-batch.
                try:
                    from beever_atlas.services.contradiction_detector import (
                        check_and_supersede,
                    )

                    if persisted_facts:

                        async def _detached_contradiction_check(
                            facts_snapshot: list[Any],
                            ch: str,
                            job: str,
                            b_idx: int,
                        ) -> None:
                            try:
                                await check_and_supersede(facts_snapshot, ch)
                            except Exception:  # noqa: BLE001
                                logger.debug(
                                    "BatchProcessor: detached contradiction check failed job_id=%s batch=%d",
                                    job,
                                    b_idx,
                                    exc_info=True,
                                )

                        asyncio.create_task(
                            _detached_contradiction_check(
                                persisted_facts, channel_id, sync_job_id, batch_index
                            )
                        )
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "BatchProcessor: contradiction detection scheduling failed job_id=%s batch=%d, continuing",
                        sync_job_id,
                        batch_index,
                        exc_info=True,
                    )

            # Extract sample data for sync history.
            raw_facts = final_state.get("extracted_facts") or {}
            facts_list = (
                raw_facts.get("facts", [])
                if isinstance(raw_facts, dict)
                else (raw_facts if isinstance(raw_facts, list) else [])
            )
            raw_entities = final_state.get("extracted_entities") or {}
            entities_list = (
                raw_entities.get("entities", []) if isinstance(raw_entities, dict) else []
            )
            rels_list = (
                raw_entities.get("relationships", []) if isinstance(raw_entities, dict) else []
            )

            batch_duration = sum(batch_stage_timings.values())
            # Embedded + media counts for the UI MetricsBar tiles.
            # Pulled here at construction time so they ride along with
            # the breakdown into ``append_batch_results_for_channel``
            # and survive activity_log eviction.
            _embedded_facts = final_state.get("embedded_facts") or []
            _preprocessed = final_state.get("preprocessed_messages") or []
            _media_count_bd = sum(
                1 for m in _preprocessed if isinstance(m, dict) and m.get("modality") == "mixed"
            )
            breakdown = BatchBreakdown(
                batch_num=batch_index,
                facts_count=len(facts_list),
                entities_count=len(entities_list),
                relationships_count=len(rels_list),
                embedded_count=len(_embedded_facts),
                media_count=_media_count_bd,
                sample_facts=[(f.get("memory_text") or "")[:120] for f in facts_list[:5]],
                sample_entities=[
                    {"name": e.get("name", "?"), "type": e.get("type", "?")}
                    for e in entities_list[:8]
                ],
                sample_relationships=[
                    {
                        "source": r.get("source", "?"),
                        "target": r.get("target", "?"),
                        "type": r.get("relationship_type", r.get("type", "?")),
                    }
                    for r in rels_list[:5]
                ],
                duration_seconds=round(batch_duration, 2),
                keys=_keys_for_batch(batch),
            )
            entities_persisted = persist_result.get("entity_count", 0) > 0

            await stores.mongodb.update_sync_progress(
                job_id=sync_job_id,
                processed=0,
                current_batch=batch_index,
                current_stage="Step 7/7 — Complete",
                stage_timings=batch_stage_timings,
                batch_result=asdict(breakdown),
            )

            if _resumed_from:
                _llm_stages = {
                    "fact_extractor",
                    "entity_extractor",
                    "classifier_agent",
                    "cross_batch_validator_agent",
                }
                _skipped_llm = len(
                    _llm_stages.intersection(set(_STAGE_ORDER[:_skipped_stage_count]))
                )
                logger.info(
                    "BatchProcessor: resumed from checkpoint '%s' (skipped %d stages, saved ~%d LLM calls) job_id=%s batch=%d",
                    _resumed_from,
                    _skipped_stage_count,
                    _skipped_llm,
                    sync_job_id,
                    batch_index,
                )

            # Atomic increment — honest counter under concurrent batch execution.
            # current_batch field keeps overwriting itself when batches run in
            # parallel, so consumers should prefer batches_completed for progress.
            await stores.mongodb.increment_batches_completed(sync_job_id)

            # ALSO bump the user-facing sync_jobs row per batch so the
            # UI's MetricsBar tiles update live instead of waiting for
            # the worker tick (15-batch claim_size) to complete. The
            # synthetic ``worker:*`` row is invisible to the frontend;
            # the channel-row is what /sync/status returns.
            if sync_job_id.startswith("worker:"):
                try:
                    await stores.mongodb.increment_batches_completed_for_channel(
                        channel_id=channel_id,
                        count=1,
                        max_batch_num=batch_index,
                    )
                    await stores.mongodb.append_batch_results_for_channel(
                        channel_id=channel_id,
                        batch_results=[asdict(breakdown)],
                    )
                    # Per-batch finalization of this batch's
                    # channel_messages rows to ``status=done``. The worker
                    # used to do this once per TICK after the whole
                    # claim finished; that left the
                    # ``MESSAGES 0/715`` counter at 0 throughout the tick
                    # even when batches were obviously finishing. Now
                    # each batch finalizes its own rows immediately, and
                    # the refresh below picks up the new "done" count.
                    # Idempotent — the EXTRACTION_STATUS_TRANSITIONS
                    # map rejects ``done → done`` so the worker's later
                    # tick-end bulk call becomes a no-op for these rows.
                    if breakdown.keys:
                        await stores.mongodb.finalize_extraction_status_bulk(
                            keys=list(breakdown.keys),
                            new_status="done",
                        )
                    await stores.mongodb.refresh_sync_progress_for_channel(channel_id)
                except Exception:
                    # Mirror to channel-row is best-effort observability —
                    # never fail a batch on a mongo blip.
                    logger.exception(
                        "BatchProcessor: channel-row mirror failed job_id=%s batch=%d channel=%s",
                        sync_job_id,
                        batch_index,
                        channel_id,
                    )

            logger.info(
                "BatchProcessor: done batch=%d/%d job_id=%s channel=%s facts=%d entities=%d",
                batch_index,
                max_batches,
                sync_job_id,
                channel_id,
                batch_facts,
                batch_entities,
            )
            # Phase 0 / Task 1.1 — sub-batch end observability event.
            logger.info(
                "BatchProcessor: subbatch_event channel=%s batch=%d stage=%s phase=end "
                "facts=%d entities=%d",
                channel_id,
                batch_index,
                "batch",
                batch_facts,
                batch_entities,
            )
            try:
                get_pipeline_events().record(
                    channel_id=channel_id,
                    stage="subbatch",
                    label=(
                        f"Batch {batch_index}/{max_batches} done — "
                        f"{batch_facts} facts, {batch_entities} entities"
                    ),
                )
            except Exception:  # noqa: BLE001
                logger.debug(
                    "BatchProcessor: pipeline_events.record end failed batch=%d",
                    batch_index,
                    exc_info=True,
                )
            batch_weaviate_ids: list[str] = list(persist_result.get("weaviate_ids") or [])
            return breakdown, batch_stage_timings, entities_persisted, batch_weaviate_ids

        # Launch all batches with bounded concurrency via as_completed.
        # Results stream in completion order; each task returns (batch_idx, payload)
        # so we don't need a fragile future→idx dict lookup.
        _semaphore_waits: list[float] = []

        async def _tagged(idx: int, b: list[Any]) -> tuple[int, Any]:
            try:
                return idx, await _run_single_batch(idx, b, list(known_entities))
            except BaseException as exc:  # noqa: BLE001
                return idx, exc

        # ``batch_index_offset`` makes the enumeration global across the
        # whole sync (see process_messages docstring). The array lookup
        # below subtracts the offset to recover the local index.
        _tasks = [_tagged(i, b) for i, b in enumerate(batches, start=batch_index_offset + 1)]

        processed_so_far = 0
        for coro in asyncio.as_completed(_tasks):
            batch_index, raw = await coro
            batch = batches[batch_index - 1 - batch_index_offset]

            if isinstance(raw, BaseException):
                if isinstance(raw, ProviderOutageError):
                    err_text = f"Provider outage: {raw}"
                    logger.error(
                        "BatchProcessor: provider outage batch=%d job_id=%s: %s",
                        batch_index,
                        sync_job_id,
                        err_text,
                    )
                else:
                    err_text = _summarize_exception(raw)  # type: ignore[arg-type]
                    logger.error(
                        "BatchProcessor: as_completed failure batch=%d job_id=%s: %s",
                        batch_index,
                        sync_job_id,
                        err_text,
                    )
                failed_breakdown = BatchBreakdown(
                    batch_num=batch_index,
                    error=err_text,
                    keys=_keys_for_batch(batch),
                )
                result.errors.append({"batch_num": batch_index, "error": err_text})
                result.batch_breakdowns.append(failed_breakdown)
            else:
                breakdown, batch_timings, entities_persisted, batch_weaviate_ids = raw
                result.batch_breakdowns.append(breakdown)
                if breakdown.error:
                    result.errors.append({"batch_num": batch_index, "error": breakdown.error})
                else:
                    result.total_facts += breakdown.facts_count
                    result.total_entities += breakdown.entities_count
                    result.total_relationships += breakdown.relationships_count
                    result.fact_ids.extend(batch_weaviate_ids)
                    for stage_key, duration in batch_timings.items():
                        cumulative_timings[stage_key] = (
                            cumulative_timings.get(stage_key, 0.0) + duration
                        )
                    if entities_persisted:
                        known_entities = await stores.entity_registry.get_all_canonical()
            processed_so_far += len(batch)
            await stores.mongodb.update_sync_progress(
                job_id=sync_job_id,
                processed=processed_so_far,
                current_batch=batch_index,
                stage_details={"cumulative_timings": cumulative_timings},
            )

        # D3 — emit semaphore_wait telemetry after all batches complete.
        if _semaphore_waits:
            _sorted_waits = sorted(_semaphore_waits)
            _p95_idx = max(0, int(len(_sorted_waits) * 0.95) - 1)
            _p95_wait = _sorted_waits[_p95_idx]
            logger.info(
                "BatchProcessor: semaphore_wait_telemetry job_id=%s batches=%d "
                "p95_wait_s=%.3f max_wait_s=%.3f concurrent_slots=%d",
                sync_job_id,
                len(_semaphore_waits),
                _p95_wait,
                _sorted_waits[-1],
                settings.ingest_batch_concurrency,
            )

        # Sort breakdowns into index order for consumers that expect it (e.g. tests, UI).
        result.batch_breakdowns.sort(key=lambda bd: bd.batch_num)

        logger.info(
            "BatchProcessor: complete job_id=%s channel=%s total_facts=%d total_entities=%d errors=%d",
            sync_job_id,
            channel_id,
            result.total_facts,
            result.total_entities,
            len(result.errors),
        )

        # ── sync_summary: structured metrics ─────────────────────────────────
        # Emit 4 log lines exactly once per sync_completed event.  These lines
        # are the PR-1 measurement substrate for the PR-2 data gate.
        # Each line is parseable by the grep one-liners in runbooks/sync-metrics.md.
        # A failed histogram query must NOT block sync completion (try/except).
        _metrics = _drain_sync_metrics(channel_id, sync_job_id)
        _rels_dropped = int(_metrics.get("relationships_dropped_total", 0))
        _trunc_recoveries = int(_metrics.get("entity_truncation_recoveries", 0))
        _lost_estimate_sum = int(_metrics.get("lost_estimate_sum", 0))
        _cbv_fallback = int(_metrics.get("cross_batch_validator_llm_fallback_total", 0))

        logger.info(
            "sync_summary: metric=relationships_dropped_total value=%d channel_id=%s sync_job_id=%s",
            _rels_dropped,
            channel_id,
            sync_job_id,
        )

        # Cluster-size histogram — query Weaviate; failures are non-fatal.
        try:
            _clusters = await stores.weaviate.list_clusters(channel_id)
            _buckets: dict[int, int] = {1: 0, 2: 0, 3: 0, 5: 0, 10: 0, 11: 0}
            for _cl in _clusters:
                _mc = getattr(_cl, "member_count", 0) or 0
                if _mc <= 1:
                    _buckets[1] += 1
                elif _mc == 2:
                    _buckets[2] += 1
                elif _mc == 3:
                    _buckets[3] += 1
                elif _mc <= 5:
                    _buckets[5] += 1
                elif _mc <= 10:
                    _buckets[10] += 1
                else:
                    _buckets[11] += 1
            _histogram = json.dumps([[k, _buckets[k]] for k in (1, 2, 3, 5, 10, 11)])
        except Exception:  # noqa: BLE001
            logger.debug(
                "sync_summary: cluster_size_histogram query failed — emitting empty",
                exc_info=True,
            )
            _histogram = "[]"

        logger.info(
            "sync_summary: metric=cluster_size_histogram value=%s channel_id=%s sync_job_id=%s",
            _histogram,
            channel_id,
            sync_job_id,
        )
        logger.info(
            "sync_summary: metric=entity_truncation_recoveries value=%d lost_estimate_sum=%d channel_id=%s sync_job_id=%s",
            _trunc_recoveries,
            _lost_estimate_sum,
            channel_id,
            sync_job_id,
        )
        logger.info(
            "sync_summary: metric=cross_batch_validator_llm_fallback_total value=%d channel_id=%s sync_job_id=%s",
            _cbv_fallback,
            channel_id,
            sync_job_id,
        )

        return result
