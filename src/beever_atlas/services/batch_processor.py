"""Batch processor — chunks messages and drives them through the ingestion pipeline.

Splits a list of NormalizedMessage objects into fixed-size batches, runs each
batch through the ADK ``ingestion_pipeline`` SequentialAgent, and accumulates
per-batch results into a final ``BatchResult``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

import httpx
from google.genai import types
from google.genai.errors import ServerError

from beever_atlas.agents.ingestion import create_ingestion_pipeline
from beever_atlas.models.sync_policy import IngestionConfig
from beever_atlas.agents.runner import create_runner, create_session
from beever_atlas.infra.config import get_settings
from beever_atlas.stores import get_stores
from beever_atlas.llm import get_llm_provider

logger = logging.getLogger(__name__)

_LLM_MAX_RETRIES = 3
_LLM_RETRY_BACKOFF = [10, 30, 60]  # seconds between retries

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
    "preprocessor", "fact_extractor", "entity_extractor",
    "embedder", "cross_batch_validator_agent", "persister",
]

_ALL_CHECKPOINT_KEYS: list[str] = [
    "preprocessed_messages", "extracted_facts", "extracted_entities",
    "classified_facts", "embedded_facts", "validated_entities",
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
    return any(
        marker in msg for marker in ("json_invalid", "max_tokens", "unexpected eof")
    )


@dataclass
class BatchBreakdown:
    """Per-batch extraction breakdown with sample data."""

    batch_num: int = 0
    facts_count: int = 0
    entities_count: int = 0
    relationships_count: int = 0
    sample_facts: list[str] = field(default_factory=list)
    sample_entities: list[dict[str, str]] = field(default_factory=list)
    sample_relationships: list[dict[str, str]] = field(default_factory=list)
    duration_seconds: float = 0.0
    error: str | None = None
    # Fact-level tracking
    facts_stored: int = 0
    facts_failed: int = 0
    facts_pending: int = 0


@dataclass
class BatchResult:
    """Accumulated result across all processed batches."""

    total_facts: int = 0
    total_entities: int = 0
    total_relationships: int = 0
    batch_breakdowns: list[BatchBreakdown] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


class BatchProcessor:
    """Chunks messages into batches and runs each through the ingestion pipeline."""

    def __init__(self) -> None:
        pass

    async def process_messages(
        self,
        messages: list[Any],
        channel_id: str,
        channel_name: str,
        sync_job_id: str,
        ingestion_config: IngestionConfig | None = None,
        use_batch_api: bool = False,
    ) -> BatchResult:
        """Process all messages in fixed-size batches.

        Args:
            messages: List of NormalizedMessage (or dict-serialisable) objects.
            channel_id: Slack/platform channel identifier.
            channel_name: Human-readable channel name.
            sync_job_id: MongoDB SyncJob ID for progress tracking.
            ingestion_config: Per-channel ingestion overrides (optional).

        Returns:
            BatchResult with accumulated fact/entity counts and any errors.
        """
        settings = get_settings()
        stores = get_stores()
        result = BatchResult()

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
                settings.batch_max_output_tokens
                if settings.batch_max_output_tokens > 0
                else None
            )
            batches = token_aware_batches(
                [m if isinstance(m, dict) else vars(m) for m in messages],
                max_tokens=settings.batch_max_prompt_tokens,
                time_window_seconds=settings.batch_time_window_seconds,
                max_output_tokens=_output_budget,
                max_facts_per_message=_max_facts_for_batching,
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

        known_entities: list[dict[str, Any]] = (
            await stores.entity_registry.get_all_canonical()
        )
        cumulative_timings: dict[str, float] = {}

        # ── Bounded-concurrency batch execution ───────────────────────────────
        sem = asyncio.Semaphore(settings.ingest_batch_concurrency)

        async def _run_single_batch(
            batch_index: int,
            batch: list[Any],
            known_entities_snapshot: list[dict[str, Any]],
        ) -> tuple[BatchBreakdown, dict[str, float], bool]:
            """Run one batch. Returns (breakdown, stage_timings, entities_were_persisted)."""
            async with sem:
                logger.info(
                    "BatchProcessor: start batch=%d/%d job_id=%s channel=%s messages=%d",
                    batch_index,
                    max_batches,
                    sync_job_id,
                    channel_id,
                    len(batch),
                )
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
                    await stores.mongodb.update_sync_progress(
                        job_id=sync_job_id,
                        processed=0,
                        current_batch=batch_index,
                        current_stage=f"Step 7/7 — Batch {batch_index} complete",
                        batch_result=asdict(breakdown),
                    )
                    return breakdown, {}, False

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
                            str(m.get("text") or m.get("content") or "")
                            for m in messages_as_dicts
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
                    "skip_graph_writes": bool(
                        ingestion_config and ingestion_config.skip_graph_writes
                    ),
                    "quality_threshold": (
                        ingestion_config.quality_threshold
                        if ingestion_config and ingestion_config.quality_threshold is not None
                        else None
                    ),
                }

                # Load checkpoint if this batch was partially processed before
                checkpoint = await stores.mongodb.load_pipeline_checkpoint(
                    sync_job_id=sync_job_id, batch_num=batch_index,
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
                        sync_job_id, batch_index, max_batches,
                        _resumed_from, _skipped_stage_count,
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
                            wait = _LLM_RETRY_BACKOFF[attempt - 1]
                            logger.warning(
                                "BatchProcessor: retrying job_id=%s batch=%d/%d "
                                "attempt=%d/%d after %ds sleep",
                                sync_job_id, batch_index, max_batches,
                                attempt + 1, _LLM_MAX_RETRIES + 1, wait,
                            )
                            await stores.mongodb.update_sync_progress(
                                job_id=sync_job_id,
                                processed=0,
                                current_batch=batch_index,
                                current_stage=f"Step 0/6 — Retrying in {wait}s (attempt {attempt + 1}/{_LLM_MAX_RETRIES + 1})",
                            )
                            await asyncio.sleep(wait)
                            # Phase 1 Step 2 (ingestion-pipeline-hardening): unconditionally
                            # re-consult the checkpoint store before every retry, regardless of
                            # which exception class triggered the retry. Without this, an
                            # httpx.HTTPStatusError from the embedder could restart from Stage 1
                            # and re-run expensive LLM fact/entity extraction that was already
                            # checkpointed. Retry count is the only gate.
                            _retry_checkpoint = await stores.mongodb.load_pipeline_checkpoint(
                                sync_job_id=sync_job_id, batch_num=batch_index,
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
                                    sync_job_id, batch_index, max_batches,
                                    attempt + 1, _resumed_from, _skipped_stage_count,
                                )
                            session = await create_session(
                                user_id="system",
                                state=initial_state,
                            )
                        batch_stage_timings = {}
                        activity_log: list[dict[str, Any]] = []
                        _logged_outputs: set[str] = set()  # Track which state keys we already logged
                        _last_stage = ""
                        _stage_start = time.monotonic()
                        _evt_count = 0
                        async for _event in runner.run_async(
                            user_id="system",
                            session_id=session.id,
                            new_message=types.Content(
                                role="user",
                                parts=[types.Part(text="process batch")],
                            ),
                        ):
                            author = getattr(_event, "author", "") or ""
                            label = _STAGE_LABELS.get(author)
                            if label and author != _last_stage:
                                if _last_stage:
                                    batch_stage_timings[_last_stage] = round(
                                        time.monotonic() - _stage_start, 2
                                    )
                                _last_stage = author
                                _stage_start = time.monotonic()
                                _provider = get_llm_provider()
                                if author == "embedder":
                                    _stage_model = _provider.embedding_model
                                elif author in ("preprocessor", "persister"):
                                    _stage_model = None
                                else:
                                    _stage_model = _provider.get_model_string(author)
                                activity_log.append({
                                    "agent": author,
                                    "stage": label,
                                    "type": "stage_start",
                                    "model": _stage_model,
                                })

                                # Save checkpoint on stage transitions
                                if _last_stage and _last_stage != "persister":
                                    try:
                                        from beever_atlas.agents.runner import get_session_service
                                        _cp_svc = get_session_service()
                                        _cp_session = await _cp_svc.get_session(
                                            app_name="beever_atlas", user_id="system", session_id=session.id,
                                        )
                                        _cp_state = _cp_session.state if _cp_session else {}
                                        _cp_snapshot = {k: _cp_state[k] for k in _ALL_CHECKPOINT_KEYS if k in _cp_state}
                                        _cp_idx = _STAGE_ORDER.index(_last_stage) if _last_stage in _STAGE_ORDER else -1
                                        await stores.mongodb.save_pipeline_checkpoint(
                                            sync_job_id=sync_job_id, batch_num=batch_index,
                                            channel_id=channel_id, completed_stage=_last_stage,
                                            completed_stage_index=_cp_idx,
                                            state_snapshot=_cp_snapshot, stage_timings=batch_stage_timings,
                                        )
                                    except Exception:
                                        logger.warning(
                                            "BatchProcessor: checkpoint save failed job_id=%s batch=%d stage=%s",
                                            sync_job_id, batch_index, _last_stage, exc_info=True,
                                        )

                            # Extract meaningful content from state_delta events.
                            # Only capture non-empty outputs to avoid duplicate/intermediate entries.
                            actions = getattr(_event, "actions", None)
                            if actions:
                                delta = getattr(actions, "state_delta", None) or getattr(actions, "stateDelta", None) or {}
                                if isinstance(delta, dict):
                                    # ── Preprocessor output ────────────────────
                                    if "preprocessed_messages" in delta and "preprocessed_messages" not in _logged_outputs:
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
                                                author = m.get("author_name") or m.get("username") or "?"
                                                full_text = m.get("text") or ""
                                                first_line = full_text.split("\n")[0][:200]
                                                badges: list[str] = []
                                                if m.get("raw_text") and m.get("raw_text") != full_text:
                                                    badges.append("COREF")
                                                    coref_count += 1
                                                if m.get("thread_context"):
                                                    badges.append("THREAD")
                                                    thread_count += 1
                                                if m.get("source_link_urls"):
                                                    badges.append(f"LINKS:{len(m['source_link_urls'])}")
                                                    link_count += 1
                                                if m.get("modality") == "mixed":
                                                    mtype = m.get("source_media_type", "")
                                                    media_icons = {"image": "🖼", "pdf": "📄", "video": "🎬", "audio": "🎵"}
                                                    icon = media_icons.get(mtype, "📎")
                                                    badges.append(f"{icon} {mtype.upper()}" if mtype else "📎 MEDIA")
                                                    media_count += 1
                                                badge_str = f" [{', '.join(badges)}]" if badges else ""
                                                msg_details.append(f"{author}: {first_line}{'…' if len(full_text.split(chr(10))[0]) > 200 else ''}{badge_str}")

                                                # Extract media observation details into structured samples
                                                # Fix: Use re.DOTALL and properly match brackets to catch [Document Digest]:
                                                doc_match = _re.search(r'\[Document (?:Digest|text)\]:?\s*(.+)', full_text, _re.DOTALL)
                                                if doc_match:
                                                    content_snippet = doc_match.group(1).strip()[:2000]
                                                    msg_details.append({"item_type": "media", "agent": "document_digester", "content": f"{content_snippet}…", "model": get_llm_provider().get_model_string("document_digester")})

                                                img_match = _re.search(r'\[Image description\]:?\s*(.+)', full_text, _re.DOTALL)
                                                if img_match:
                                                    content_snippet = img_match.group(1).strip().split("\n")[0][:500]
                                                    msg_details.append({"item_type": "media", "agent": "image_describer", "content": f"{content_snippet}…", "model": get_llm_provider().get_model_string("image_describer")})
                                                if not img_match:
                                                    img_meta = _re.search(r'\[Attachment:.*?\(image', full_text)
                                                    if img_meta:
                                                        msg_details.append({"item_type": "media", "agent": "image_describer", "content": "Vision skipped (message text sufficient)", "model": get_llm_provider().get_model_string("image_describer"), "status": "skipped"})

                                                vid_match = _re.search(r'\[Video (?:summary|transcript|analysis)\]:?\s*(.+)', full_text, _re.DOTALL)
                                                if vid_match:
                                                    content_snippet = vid_match.group(1).strip()[:2000]
                                                    msg_details.append({"item_type": "media", "agent": "video_describer", "content": f"{content_snippet}…", "model": get_llm_provider().get_model_string("video_analyzer")})

                                                vid_vis = _re.search(r'\[Video visual description\]:?\s*(.+)', full_text, _re.DOTALL)
                                                if vid_vis:
                                                    content_snippet = vid_vis.group(1).strip()[:2000]
                                                    msg_details.append({"item_type": "media", "agent": "video_describer", "content": f"{content_snippet}…", "model": get_llm_provider().get_model_string("video_analyzer")})

                                                aud_match = _re.search(r'\[Audio (?:summary|transcript)\]:?\s*(.+)', full_text, _re.DOTALL)
                                                if aud_match:
                                                    content_snippet = aud_match.group(1).strip()[:2000]
                                                    msg_details.append({"item_type": "media", "agent": "audio_describer", "content": f"{content_snippet}…", "model": get_llm_provider().get_model_string("audio_transcriber")})

                                                # Detect failed media agents (timeout or other errors)
                                                if m.get("modality") == "mixed":
                                                    img_meta = _re.search(r'\[Attachment:.*?\(image', full_text)
                                                    has_media_output = bool(doc_match or img_match or img_meta or vid_match or vid_vis or aud_match)
                                                    if not has_media_output:
                                                        media_type = m.get("source_media_type", "")
                                                        agent_map = {"pdf": "document_digester", "image": "image_describer", "video": "video_analyzer", "audio": "audio_transcriber"}
                                                        model_map = {"pdf": "document_digester", "image": "image_describer", "video": "video_analyzer", "audio": "audio_transcriber"}
                                                        agent = agent_map.get(media_type, "media_processor")
                                                        model_key = model_map.get(media_type, "document_digester")
                                                        media_names = m.get("source_media_names", [])
                                                        file_name = media_names[0] if media_names else "unknown"
                                                        is_timeout = "processing timed out" in full_text.lower()
                                                        msg_details.append({
                                                            "item_type": "media",
                                                            "agent": agent,
                                                            "content": f"Processing timed out for {file_name}" if is_timeout else f"Processing failed for {file_name}",
                                                            "model": get_llm_provider().get_model_string(model_key),
                                                            "status": "timeout" if is_timeout else "error",
                                                        })

                                                msg_details.append({
                                                    "item_type": "message",
                                                    "author": author,
                                                    "content": f"{first_line}{'…' if len(full_text.split(chr(10))[0]) > 200 else ''}",
                                                    "tags": badges
                                                })

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
                                            activity_log.append({
                                                "type": "stage_output", "agent": "preprocessor",
                                                "message": " · ".join(summary_parts),
                                                "metrics": {
                                                    "messages": len(msgs),
                                                    "media": media_count
                                                },
                                                "samples": msg_details[:20],
                                                "elapsed": elapsed,
                                            })

                                    # ── Fact extraction output ─────────────────
                                    if "extracted_facts" in delta and "extracted_facts" not in _logged_outputs:
                                        raw = delta["extracted_facts"]
                                        facts_list = raw.get("facts", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                                        if facts_list:
                                            _logged_outputs.add("extracted_facts")
                                            fact_summaries = []
                                            for f in facts_list[:5]:
                                                text = (f.get("memory_text") or "")[:300]
                                                score = f.get("quality_score", 0)
                                                imp = f.get("importance", "?")
                                                fact_summaries.append({
                                                    "item_type": "fact",
                                                    "content": text,
                                                    "score": score,
                                                    "tags": [imp]
                                                })
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            avg_quality = sum(f.get('quality_score', 0) for f in facts_list) / len(facts_list) if facts_list else 0
                                            activity_log.append({
                                                "type": "stage_output", "agent": "fact_extractor",
                                                "message": f"Extracted {len(facts_list)} facts (avg quality {avg_quality:.2f})",
                                                "model": get_llm_provider().get_model_string("fact_extractor"),
                                                "metrics": {
                                                    "count": len(facts_list),
                                                    "avg_quality": float(f"{avg_quality:.2f}")
                                                },
                                                "samples": fact_summaries,
                                                "elapsed": elapsed,
                                            })

                                    # ── Entity extraction output ───────────────
                                    if "extracted_entities" in delta and "extracted_entities" not in _logged_outputs:
                                        raw = delta["extracted_entities"]
                                        entities = raw.get("entities", []) if isinstance(raw, dict) else []
                                        rels = raw.get("relationships", []) if isinstance(raw, dict) else []
                                        if entities or rels:
                                            _logged_outputs.add("extracted_entities")
                                            entity_details = [
                                                {"item_type": "entity", "content": e.get('name', '?'), "tags": [e.get('type', '?')]} 
                                                for e in entities[:8]
                                            ]
                                            rel_details = [
                                                {"item_type": "relationship", "source": r.get('source', '?'), "rel_type": r.get('type', '?'), "target": r.get('target', '?')}
                                                for r in rels[:5]
                                            ]
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            activity_log.append({
                                                "type": "stage_output", "agent": "entity_extractor",
                                                "message": f"Found {len(entities)} entities, {len(rels)} relationships",
                                                "model": get_llm_provider().get_model_string("entity_extractor"),
                                                "metrics": {
                                                    "entities": len(entities),
                                                    "relationships": len(rels)
                                                },
                                                "samples": entity_details + rel_details,
                                                "elapsed": elapsed,
                                            })

                                    # ── Embedder output ────────────────────────
                                    if "embedded_facts" in delta and "embedded_facts" not in _logged_outputs:
                                        embedded = delta["embedded_facts"]
                                        count = len(embedded) if isinstance(embedded, list) else 0
                                        if count > 0:
                                            _logged_outputs.add("embedded_facts")
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            activity_log.append({
                                                "type": "stage_output", "agent": "embedder",
                                                "message": f"Embedded {count} facts",
                                                "model": get_llm_provider().embedding_model,
                                                "metrics": {"embedded": count},
                                                "elapsed": elapsed,
                                            })

                                    # ── Validator output ───────────────────────
                                    if "validated_entities" in delta and "validated_entities" not in _logged_outputs:
                                        raw = delta["validated_entities"]
                                        entities = raw.get("entities", []) if isinstance(raw, dict) else []
                                        merges = raw.get("merges", []) if isinstance(raw, dict) else []
                                        if entities or merges:
                                            _logged_outputs.add("validated_entities")
                                            merge_details = [
                                                {"item_type": "validation", "content": f"{', '.join(mg['merged_from']) if isinstance(mg.get('merged_from'), list) else mg.get('merged_from', '?')} → {mg.get('canonical', '?')}"}
                                                for mg in merges[:5]
                                            ]
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            activity_log.append({
                                                "type": "stage_output", "agent": "cross_batch_validator_agent",
                                                "message": f"Validated {len(entities)} entities" + (f", {len(merges)} merges" if merges else ""),
                                                "model": get_llm_provider().get_model_string("cross_batch_validator_agent"),
                                                "metrics": {"entities": len(entities), "merges": len(merges)},
                                                "samples": merge_details if merge_details else None,
                                                "elapsed": elapsed,
                                            })

                                    # ── Persister output ───────────────────────
                                    if "persist_result" in delta and "persist_result" not in _logged_outputs:
                                        pr = delta["persist_result"]
                                        wv_count = len(pr.get("weaviate_ids", []))
                                        neo_count = pr.get("entity_count", 0)
                                        rel_count = pr.get("relationship_count", 0)
                                        if wv_count > 0 or neo_count > 0:
                                            _logged_outputs.add("persist_result")
                                            elapsed = round(time.monotonic() - _stage_start, 1)
                                            activity_log.append({
                                                "type": "stage_output", "agent": "persister",
                                                "message": f"Saved {wv_count} facts → Weaviate, {neo_count} entities + {rel_count} rels → Neo4j",
                                                "metrics": {"weaviate_facts": wv_count, "neo4j_entities": neo_count, "neo4j_rels": rel_count},
                                                "elapsed": elapsed,
                                            })

                            # Throttle MongoDB updates — only write on stage changes or every 5 events
                            _evt_count += 1
                            should_update = bool(label) or _evt_count % 5 == 0
                            if should_update or label:
                                await stores.mongodb.update_sync_progress(
                                    job_id=sync_job_id,
                                    processed=0,
                                    current_batch=batch_index,
                                    current_stage=label if label else None,
                                    stage_timings=batch_stage_timings,
                                    stage_details={"activity_log": activity_log[-50:]},
                                )

                        if _last_stage:
                            batch_stage_timings[_last_stage] = round(
                                time.monotonic() - _stage_start, 2
                            )
                        # Final progress flush after pipeline completes
                        await stores.mongodb.update_sync_progress(
                            job_id=sync_job_id,
                            processed=0,
                            current_batch=batch_index,
                            current_stage=f"Step 7/7 — Batch {batch_index} complete",
                            stage_timings=batch_stage_timings,
                            stage_details={"activity_log": activity_log[-50:]},
                        )
                        # Clean up checkpoint after successful completion
                        try:
                            await stores.mongodb.delete_pipeline_checkpoint(
                                sync_job_id=sync_job_id, batch_num=batch_index,
                            )
                        except Exception:
                            pass
                        break  # success
                    except (ServerError, httpx.HTTPStatusError) as exc:
                        # Phase 1 Step 2: httpx.HTTPStatusError (e.g. embedder 500) is
                        # retryable just like ServerError. Checkpoint resume (above) ensures
                        # completed stages are not re-run on retry.
                        is_http_5xx = (
                            isinstance(exc, httpx.HTTPStatusError)
                            and exc.response.status_code >= 500
                        )
                        if not isinstance(exc, ServerError) and not is_http_5xx:
                            raise
                        if attempt < _LLM_MAX_RETRIES:
                            logger.warning(
                                "BatchProcessor: transient error job_id=%s batch=%d/%d "
                                "attempt=%d/%d: %s",
                                sync_job_id, batch_index, max_batches,
                                attempt + 1, _LLM_MAX_RETRIES + 1, exc,
                            )
                            # Sleep and retry happen at the top of the next loop iteration
                        else:
                            raise
                    except Exception as exc:
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
                                    sync_job_id, batch_index, max_batches,
                                    attempt + 1, _LLM_MAX_RETRIES + 1,
                                    len(current_msgs), str(exc)[:200],
                                )
                            elif len(current_msgs) > 5:
                                # Second: halve the batch (remaining messages will be missed
                                # but the batch won't crash — user can re-sync to catch them)
                                half = len(current_msgs) // 2
                                initial_state["messages"] = current_msgs[:half]
                                logger.warning(
                                    "BatchProcessor: LLM still truncating job_id=%s batch=%d/%d "
                                    "attempt=%d/%d — halving batch from %d to %d messages: %s",
                                    sync_job_id, batch_index, max_batches,
                                    attempt + 1, _LLM_MAX_RETRIES + 1,
                                    len(current_msgs), half, str(exc)[:200],
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
                final_state: dict[str, Any] = (
                    final_session.state if final_session else {}
                )
                persist_result: dict[str, Any] = (
                    final_state.get("persist_result") or {}
                )
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
                # Runs AFTER persistence completes, outside the outbox transaction.
                try:
                    from beever_atlas.services.contradiction_detector import check_and_supersede
                    embedded_facts_raw = final_state.get("embedded_facts") or []
                    if embedded_facts_raw:
                        from beever_atlas.models import AtomicFact
                        persisted_facts: list[AtomicFact] = []
                        weaviate_ids = persist_result.get("weaviate_ids") or []
                        for idx, fd in enumerate(embedded_facts_raw):
                            fact_channel = fd.get("channel_id") or channel_id
                            msg_ts = fd.get("message_ts", "")
                            fact_id = (
                                weaviate_ids[idx] if idx < len(weaviate_ids)
                                else AtomicFact.deterministic_id("slack", fact_channel, msg_ts, idx)
                            )
                            persisted_facts.append(AtomicFact(
                                id=fact_id,
                                memory_text=fd.get("memory_text", ""),
                                topic_tags=fd.get("topic_tags") or [],
                                entity_tags=fd.get("entity_tags") or [],
                                channel_id=fact_channel,
                            ))
                        await check_and_supersede(persisted_facts, channel_id)
                except Exception:  # noqa: BLE001
                    logger.debug(
                        "BatchProcessor: contradiction detection failed job_id=%s batch=%d, continuing",
                        sync_job_id, batch_index, exc_info=True,
                    )

                # Extract sample data for sync history.
                raw_facts = final_state.get("extracted_facts") or {}
                facts_list = raw_facts.get("facts", []) if isinstance(raw_facts, dict) else (raw_facts if isinstance(raw_facts, list) else [])
                raw_entities = final_state.get("extracted_entities") or {}
                entities_list = raw_entities.get("entities", []) if isinstance(raw_entities, dict) else []
                rels_list = raw_entities.get("relationships", []) if isinstance(raw_entities, dict) else []

                batch_duration = sum(batch_stage_timings.values())
                breakdown = BatchBreakdown(
                    batch_num=batch_index,
                    facts_count=len(facts_list),
                    entities_count=len(entities_list),
                    relationships_count=len(rels_list),
                    sample_facts=[
                        (f.get("memory_text") or "")[:120] for f in facts_list[:5]
                    ],
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
                )
                entities_persisted = persist_result.get("entity_count", 0) > 0

                await stores.mongodb.update_sync_progress(
                    job_id=sync_job_id,
                    processed=0,
                    current_batch=batch_index,
                    current_stage="Step 7/7 — Complete",
                    stage_timings=batch_stage_timings,
                    stage_details={"activity_log": activity_log[-50:]},
                    batch_result=asdict(breakdown),
                )

                if _resumed_from:
                    _llm_stages = {"fact_extractor", "entity_extractor", "classifier_agent", "cross_batch_validator_agent"}
                    _skipped_llm = len(_llm_stages.intersection(set(_STAGE_ORDER[:_skipped_stage_count])))
                    logger.info(
                        "BatchProcessor: resumed from checkpoint '%s' (skipped %d stages, saved ~%d LLM calls) job_id=%s batch=%d",
                        _resumed_from, _skipped_stage_count, _skipped_llm, sync_job_id, batch_index,
                    )

                # Atomic increment — honest counter under concurrent batch execution.
                # current_batch field keeps overwriting itself when batches run in
                # parallel, so consumers should prefer batches_completed for progress.
                await stores.mongodb.increment_batches_completed(sync_job_id)

                logger.info(
                    "BatchProcessor: done batch=%d/%d job_id=%s channel=%s facts=%d entities=%d",
                    batch_index,
                    max_batches,
                    sync_job_id,
                    channel_id,
                    batch_facts,
                    batch_entities,
                )
                return breakdown, batch_stage_timings, entities_persisted

        # Launch all batches with bounded concurrency; gather preserves index order.
        batch_tasks = [
            _run_single_batch(i, b, list(known_entities))
            for i, b in enumerate(batches, start=1)
        ]
        raw_results = await asyncio.gather(*batch_tasks, return_exceptions=True)

        processed_so_far = 0
        for idx, raw in enumerate(raw_results):
            batch = batches[idx]
            batch_index = idx + 1
            if isinstance(raw, BaseException):
                err_text = _summarize_exception(raw)  # type: ignore[arg-type]
                logger.error(
                    "BatchProcessor: unexpected gather failure batch=%d job_id=%s: %s",
                    batch_index, sync_job_id, err_text,
                )
                failed_breakdown = BatchBreakdown(batch_num=batch_index, error=err_text)
                result.errors.append({"batch_num": batch_index, "error": err_text})
                result.batch_breakdowns.append(failed_breakdown)
            else:
                breakdown, batch_timings, entities_persisted = raw
                result.batch_breakdowns.append(breakdown)
                if breakdown.error:
                    result.errors.append({"batch_num": batch_index, "error": breakdown.error})
                else:
                    result.total_facts += breakdown.facts_count
                    result.total_entities += breakdown.entities_count
                    result.total_relationships += breakdown.relationships_count
                    for stage_key, duration in batch_timings.items():
                        cumulative_timings[stage_key] = cumulative_timings.get(stage_key, 0.0) + duration
                    if entities_persisted:
                        known_entities = await stores.entity_registry.get_all_canonical()
            processed_so_far += len(batch)
            await stores.mongodb.update_sync_progress(
                job_id=sync_job_id,
                processed=processed_so_far,
                current_batch=batch_index,
                stage_details={"cumulative_timings": cumulative_timings},
            )

        logger.info(
            "BatchProcessor: complete job_id=%s channel=%s total_facts=%d total_entities=%d errors=%d",
            sync_job_id,
            channel_id,
            result.total_facts,
            result.total_entities,
            len(result.errors),
        )
        return result
