"""Batch processor — chunks messages and drives them through the ingestion pipeline.

Splits a list of NormalizedMessage objects into fixed-size batches, runs each
batch through the ADK ``ingestion_pipeline`` SequentialAgent, and accumulates
per-batch results into a final ``BatchResult``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from google.genai import types
from google.genai.errors import ServerError

from beever_atlas.agents.ingestion import create_ingestion_pipeline
from beever_atlas.agents.runner import create_runner, create_session
from beever_atlas.infra.config import get_settings
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

_LLM_MAX_RETRIES = 3
_LLM_RETRY_BACKOFF = [10, 30, 60]  # seconds between retries

# Map ADK agent names to human-readable stage descriptions with step numbers.
_STAGE_LABELS: dict[str, str] = {
    "preprocessor": "Step 1/7 — Preprocessing messages",
    "fact_extractor": "Step 2/7 — Extracting facts (LLM)",
    "entity_extractor": "Step 2/7 — Extracting entities (LLM)",
    "classifier_agent": "Step 3/7 — Classifying facts (LLM)",
    "embedder": "Step 4/7 — Generating embeddings",
    "cross_batch_validator_agent": "Step 5/7 — Validating entities",
    "persister": "Step 6/7 — Saving to stores",
}


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
    ) -> BatchResult:
        """Process all messages in fixed-size batches.

        Args:
            messages: List of NormalizedMessage (or dict-serialisable) objects.
            channel_id: Slack/platform channel identifier.
            channel_name: Human-readable channel name.
            sync_job_id: MongoDB SyncJob ID for progress tracking.

        Returns:
            BatchResult with accumulated fact/entity counts and any errors.
        """
        settings = get_settings()
        stores = get_stores()
        result = BatchResult()

        batch_size = settings.sync_batch_size
        batch_timeout = settings.sync_batch_timeout_seconds
        total = len(messages)
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

        processed_so_far = 0

        for batch_num, batch in enumerate(batches):
            batch_index = batch_num + 1
            try:
                # Re-fetch known entities each batch so later batches see
                # entities created by earlier ones in the same run.
                known_entities: list[dict[str, Any]] = (
                    await stores.entity_registry.get_all_canonical()
                )
                logger.info(
                    "BatchProcessor: start job_id=%s channel=%s batch=%d/%d messages=%d",
                    sync_job_id,
                    channel_id,
                    batch_index,
                    max_batches,
                    len(batch),
                )
                # Mark batch start immediately so UI doesn't stay on batch 0 while
                # long-running LLM/vector writes are in flight.
                await stores.mongodb.update_sync_progress(
                    job_id=sync_job_id,
                    processed=processed_so_far,
                    current_batch=batch_index,
                )
                # Convert NormalizedMessage objects to plain dicts for session state.
                messages_as_dicts: list[dict[str, Any]] = [
                    m if isinstance(m, dict) else vars(m) for m in batch
                ]

                initial_state: dict[str, Any] = {
                    "messages": messages_as_dicts,
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "batch_num": batch_index,
                    "max_facts_per_message": settings.max_facts_per_message,
                    "known_entities": known_entities,
                    "sync_job_id": sync_job_id,
                }

                session = await create_session(
                    user_id="system",
                    state=initial_state,
                )

                # Drive the pipeline to completion with retry on transient LLM errors.
                batch_stage_timings: dict[str, float] = {}
                for attempt in range(_LLM_MAX_RETRIES + 1):
                    try:
                        # Each retry needs a fresh session since the pipeline
                        # may have partially mutated the previous one.
                        if attempt > 0:
                            session = await create_session(
                                user_id="system",
                                state=initial_state,
                            )
                        async with asyncio.timeout(batch_timeout):
                            batch_stage_timings = {}
                            activity_log: list[dict[str, Any]] = []
                            _last_stage = ""
                            _stage_start = time.monotonic()
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
                                    activity_log.append({
                                        "agent": author,
                                        "stage": label,
                                        "type": "stage_start",
                                    })

                                # Extract meaningful content from events
                                actions = getattr(_event, "actions", None)
                                if actions:
                                    delta = getattr(actions, "state_delta", None) or getattr(actions, "stateDelta", None) or {}
                                    if isinstance(delta, dict):
                                        # Preprocessor output
                                        if "preprocessed_messages" in delta:
                                            msgs = delta["preprocessed_messages"]
                                            count = len(msgs) if isinstance(msgs, list) else 0
                                            activity_log.append({
                                                "type": "output", "agent": "preprocessor",
                                                "message": f"Retained {count} messages",
                                            })
                                        # Fact extraction output
                                        if "extracted_facts" in delta:
                                            raw = delta["extracted_facts"]
                                            facts_list = raw.get("facts", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
                                            fact_summaries = []
                                            for f in facts_list[:5]:
                                                text = (f.get("memory_text") or "")[:80]
                                                score = f.get("quality_score", 0)
                                                fact_summaries.append(f"[{score:.1f}] {text}")
                                            activity_log.append({
                                                "type": "output", "agent": "fact_extractor",
                                                "message": f"Extracted {len(facts_list)} facts",
                                                "details": fact_summaries,
                                            })
                                        # Entity extraction output
                                        if "extracted_entities" in delta:
                                            raw = delta["extracted_entities"]
                                            entities = raw.get("entities", []) if isinstance(raw, dict) else []
                                            rels = raw.get("relationships", []) if isinstance(raw, dict) else []
                                            entity_names = [e.get("name", "?") for e in entities[:8]]
                                            activity_log.append({
                                                "type": "output", "agent": "entity_extractor",
                                                "message": f"Found {len(entities)} entities, {len(rels)} relationships",
                                                "details": entity_names,
                                            })
                                        # Embedder output
                                        if "embedded_facts" in delta:
                                            embedded = delta["embedded_facts"]
                                            count = len(embedded) if isinstance(embedded, list) else 0
                                            activity_log.append({
                                                "type": "output", "agent": "embedder",
                                                "message": f"Embedded {count} facts",
                                            })
                                        # Persister output
                                        if "persist_result" in delta:
                                            pr = delta["persist_result"]
                                            activity_log.append({
                                                "type": "output", "agent": "persister",
                                                "message": f"Saved {len(pr.get('weaviate_ids', []))} facts to Weaviate, {pr.get('entity_count', 0)} entities to Neo4j",
                                            })

                                    await stores.mongodb.update_sync_progress(
                                        job_id=sync_job_id,
                                        processed=processed_so_far,
                                        current_batch=batch_index,
                                        current_stage=label if label else None,
                                        stage_timings=batch_stage_timings,
                                        stage_details={"activity_log": activity_log[-10:]},  # keep last 10 entries
                                    )

                            if _last_stage:
                                batch_stage_timings[_last_stage] = round(
                                    time.monotonic() - _stage_start, 2
                                )
                        break  # success
                    except TimeoutError as exc:
                        raise TimeoutError(
                            f"Ingestion batch timed out after {batch_timeout}s "
                            f"(batch={batch_index}/{max_batches}, messages={len(batch)})"
                        ) from exc
                    except ServerError as exc:
                        if attempt < _LLM_MAX_RETRIES:
                            wait = _LLM_RETRY_BACKOFF[attempt]
                            logger.warning(
                                "BatchProcessor: LLM 503/transient error job_id=%s batch=%d/%d "
                                "attempt=%d/%d retrying in %ds: %s",
                                sync_job_id,
                                batch_index,
                                max_batches,
                                attempt + 1,
                                _LLM_MAX_RETRIES + 1,
                                wait,
                                exc,
                            )
                            await stores.mongodb.update_sync_progress(
                                job_id=sync_job_id,
                                processed=processed_so_far,
                                current_batch=batch_index,
                                current_stage=f"LLM unavailable — retrying in {wait}s (attempt {attempt + 1}/{_LLM_MAX_RETRIES + 1})",
                            )
                            await asyncio.sleep(wait)
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
                result.batch_breakdowns.append(breakdown)

                result.total_facts += batch_facts
                result.total_entities += batch_entities
                result.total_relationships += len(rels_list)

                processed_so_far += len(batch)

                await stores.mongodb.update_sync_progress(
                    job_id=sync_job_id,
                    processed=processed_so_far,
                    current_batch=batch_index,
                    current_stage="Step 7/7 — Complete",
                    stage_timings=batch_stage_timings,
                )

                logger.info(
                    "BatchProcessor: done job_id=%s channel=%s batch=%d/%d facts=%d entities=%d processed=%d/%d",
                    sync_job_id,
                    channel_id,
                    batch_index,
                    max_batches,
                    batch_facts,
                    batch_entities,
                    processed_so_far,
                    total,
                )

            except Exception as exc:  # noqa: BLE001
                err_text = _summarize_exception(exc)
                logger.error(
                    "BatchProcessor: failed job_id=%s channel=%s batch=%d/%d error=%s",
                    sync_job_id,
                    channel_id,
                    batch_index,
                    max_batches,
                    err_text,
                    exc_info=True,
                )
                result.errors.append(
                    {
                        "batch_num": batch_index,
                        "error": err_text,
                    }
                )
                # Advance progress even on failure so the UI doesn't stay stuck.
                processed_so_far += len(batch)
                await stores.mongodb.update_sync_progress(
                    job_id=sync_job_id,
                    processed=processed_so_far,
                    current_batch=batch_index,
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
