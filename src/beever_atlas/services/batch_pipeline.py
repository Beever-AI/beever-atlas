"""Batch pipeline orchestrator using Gemini Batch API for extraction stages.

Replaces the ADK SequentialAgent pipeline for batch mode. Runs stages manually:
  1. Preprocess   — PreprocessorAgent in-process via ADK runner
  2. Batch Extract — fact + entity extraction via parallel Gemini Batch API jobs
  3. Quality Gates — filter facts/entities by configured thresholds
  4. Enrich        — EmbedderAgent + cross_batch_validator in-process via ADK
  5. Persist       — PersisterAgent in-process via ADK runner

Progress is written to MongoDB in the same format as BatchProcessor so the UI
sees a consistent activity_log and stage_details regardless of which path ran.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from google.adk.agents import ParallelAgent, SequentialAgent

from beever_atlas.agents.ingestion.cross_batch_validator import (
    create_cross_batch_validator,
)
from beever_atlas.agents.ingestion.embedder import EmbedderAgent
from beever_atlas.agents.ingestion.persister import PersisterAgent
from beever_atlas.agents.ingestion.preprocessor import PreprocessorAgent
from beever_atlas.agents.prompts.entity_extractor import ENTITY_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.prompts.fact_extractor import FACT_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.runner import create_runner, create_session, get_session_service
from beever_atlas.infra.config import get_settings
from beever_atlas.llm import get_llm_provider
from beever_atlas.models.sync_policy import IngestionConfig
from beever_atlas.services.batch_processor import BatchBreakdown
from beever_atlas.services.gemini_batch import BatchRequest, GeminiBatchClient
from beever_atlas.services.json_recovery import (
    recover_entities_from_truncated,
    recover_facts_from_truncated,
)
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

_APP_NAME = "beever_atlas"

# Stage label strings — kept in sync with BatchProcessor._STAGE_LABELS
_STAGE_LABELS = {
    "preprocessor": "Step 1/6 — Preprocessing messages",
    "fact_extractor": "Step 2/6 — Extracting facts (Batch API: processing)",
    "entity_extractor": "Step 3/6 — Extracting entities (Batch API: processing)",
    "embedder": "Step 4/6 — Generating embeddings",
    "cross_batch_validator_agent": "Step 5/6 — Validating entities",
    "persister": "Step 6/6 — Saving to stores",
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_fact_prompt(
    channel_name: str,
    preprocessed_messages_json: str,
    max_facts_per_message: int,
) -> str:
    """Format the fact extractor instruction template."""
    return FACT_EXTRACTOR_INSTRUCTION.format(
        channel_name=channel_name,
        preprocessed_messages=preprocessed_messages_json,
        max_facts_per_message=max_facts_per_message,
    )


def _build_entity_prompt(
    channel_name: str,
    channel_id: str,
    known_entities_json: str,
    preprocessed_messages_json: str,
) -> str:
    """Format the entity extractor instruction template."""
    return ENTITY_EXTRACTOR_INSTRUCTION.format(
        channel_name=channel_name,
        channel_id=channel_id,
        known_entities=known_entities_json,
        preprocessed_messages=preprocessed_messages_json,
    )


def _apply_fact_quality_gate(
    raw: dict[str, Any],
    threshold: float,
) -> dict[str, Any]:
    """Drop facts below quality threshold. Returns a new dict with filtered facts."""
    facts_dicts: list[dict[str, Any]] = raw.get("facts") or []
    before = len(facts_dicts)
    filtered = [f for f in facts_dicts if f.get("quality_score", 0.0) >= threshold]
    after = len(filtered)
    if before != after:
        logger.info(
            "batch_pipeline: fact quality gate dropped %d/%d facts below threshold %.2f",
            before - after,
            before,
            threshold,
        )
    return {**raw, "facts": filtered}


def _apply_entity_quality_gate(
    raw: dict[str, Any],
    threshold: float,
) -> dict[str, Any]:
    """Drop relationships and orphaned channel-scoped entities below threshold."""
    entities_dicts: list[dict[str, Any]] = raw.get("entities") or []
    rels_dicts: list[dict[str, Any]] = raw.get("relationships") or []
    skip_reason: str | None = raw.get("skip_reason")

    rels_before = len(rels_dicts)
    filtered_rels = [r for r in rels_dicts if r.get("confidence", 0.0) >= threshold]
    rels_after = len(filtered_rels)
    if rels_before != rels_after:
        logger.info(
            "batch_pipeline: entity quality gate dropped %d/%d relationships below threshold %.2f",
            rels_before - rels_after,
            rels_before,
            threshold,
        )

    surviving_names: set[str] = set()
    for r in filtered_rels:
        surviving_names.add(r.get("source", ""))
        surviving_names.add(r.get("target", ""))

    entities_before = len(entities_dicts)
    filtered_entities = [
        e
        for e in entities_dicts
        if e.get("scope") == "global" or e.get("name", "") in surviving_names
    ]
    entities_after = len(filtered_entities)
    if entities_before != entities_after:
        logger.info(
            "batch_pipeline: entity quality gate dropped %d/%d channel-scoped entities",
            entities_before - entities_after,
            entities_before,
        )

    return {
        "entities": filtered_entities,
        "relationships": filtered_rels,
        "skip_reason": skip_reason,
    }


async def _run_adk_agent(
    agent: Any,
    state: dict[str, Any],
) -> dict[str, Any]:
    """Run an ADK agent in-process and return the final session state."""
    from google.genai import types

    runner = create_runner(agent)
    session = await create_session(user_id="system", state=state)

    async for _event in runner.run_async(
        user_id="system",
        session_id=session.id,
        new_message=types.Content(
            role="user",
            parts=[types.Part(text="process batch")],
        ),
    ):
        pass  # Drive to completion

    svc = get_session_service()
    final = await svc.get_session(
        app_name=_APP_NAME,
        user_id="system",
        session_id=session.id,
    )
    return dict(final.state) if final else {}


# ---------------------------------------------------------------------------
# BatchPipelineRunner
# ---------------------------------------------------------------------------


class BatchPipelineRunner:
    """Orchestrates ingestion using Gemini Batch API for extraction stages.

    Runs the six ingestion stages manually, replacing the ADK SequentialAgent
    for batch mode. Extraction (stages 2 & 3) is submitted as parallel Gemini
    Batch API jobs. All other stages run in-process via ADK runners.
    """

    async def process_batch(
        self,
        messages: list[dict],
        channel_id: str,
        channel_name: str,
        sync_job_id: str,
        batch_num: int,
        max_batches: int,
        known_entities: list[dict],
        ingestion_config: IngestionConfig | None = None,
    ) -> BatchBreakdown:
        """Process one batch of messages through the pipeline.

        Args:
            messages: Raw message dicts for this batch.
            channel_id: Platform channel identifier.
            channel_name: Human-readable channel name.
            sync_job_id: MongoDB SyncJob ID for progress tracking.
            batch_num: 1-based index of this batch.
            max_batches: Total number of batches in the sync run.
            known_entities: Canonical entity registry from prior batches.
            ingestion_config: Per-channel ingestion overrides (optional).

        Returns:
            BatchBreakdown with counts and sample data for this batch.
        """
        settings = get_settings()
        stores = get_stores()
        provider = get_llm_provider()

        _max_facts = (
            ingestion_config.max_facts_per_message
            if ingestion_config and ingestion_config.max_facts_per_message is not None
            else settings.max_facts_per_message
        )
        quality_threshold: float = (
            ingestion_config.quality_threshold
            if ingestion_config and ingestion_config.quality_threshold is not None
            else settings.quality_threshold
        )
        skip_entity_extraction = bool(ingestion_config and ingestion_config.skip_entity_extraction)

        activity_log: list[dict[str, Any]] = []
        stage_timings: dict[str, float] = {}
        breakdown = BatchBreakdown(batch_num=batch_num)

        # ── Helpers ────────────────────────────────────────────────────────

        async def _update_progress(stage: str) -> None:
            await stores.mongodb.update_sync_progress(
                job_id=sync_job_id,
                processed=0,
                current_batch=batch_num,
                total_batches=max_batches,
                current_stage=stage,
                stage_timings=stage_timings,
                stage_details={"activity_log": activity_log[-50:]},
            )

        # ── Stage 1: Preprocess ────────────────────────────────────────────
        stage1_label = _STAGE_LABELS["preprocessor"]
        activity_log.append(
            {
                "type": "stage_start",
                "agent": "preprocessor",
                "stage": stage1_label,
                "model": None,
            }
        )
        await _update_progress(stage1_label)

        t0 = time.monotonic()
        preprocess_state = await _run_adk_agent(
            SequentialAgent(
                name="batch_preprocessor",
                sub_agents=[PreprocessorAgent(name="preprocessor")],
            ),
            state={
                "messages": messages,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "batch_num": batch_num,
                "sync_job_id": sync_job_id,
                "max_facts_per_message": _max_facts,
                "known_entities": known_entities,
                "skip_entity_extraction": skip_entity_extraction,
                "skip_graph_writes": bool(ingestion_config and ingestion_config.skip_graph_writes),
                "quality_threshold": quality_threshold,
            },
        )
        stage_timings["preprocessor"] = round(time.monotonic() - t0, 2)

        preprocessed_messages: list[dict] = preprocess_state.get("preprocessed_messages") or []

        # Build detailed preprocessor samples including media agent activity
        import re as _re

        preprocess_samples: list[dict[str, Any]] = []
        media_count = 0
        for m in preprocessed_messages[:15]:
            author = m.get("author_name") or m.get("username") or "?"
            full_text = m.get("text") or ""
            first_line = full_text.split("\n")[0][:200]
            badges: list[str] = []
            if m.get("modality") == "mixed":
                mtype = m.get("source_media_type", "")
                media_icons = {"image": "🖼", "pdf": "📄", "video": "🎬", "audio": "🎵"}
                icon = media_icons.get(mtype, "📎")
                badges.append(f"{icon} {mtype.upper()}" if mtype else "📎 MEDIA")
                media_count += 1

            # Extract media agent observations from enriched text
            doc_match = _re.search(r"\[Document (?:Digest|text)\]:?\s*(.+)", full_text, _re.DOTALL)
            if doc_match:
                snippet = doc_match.group(1).strip()[:2000]
                preprocess_samples.append(
                    {
                        "item_type": "media",
                        "agent": "document_digester",
                        "content": f"{snippet}…",
                        "model": provider.get_model_string("document_digester"),
                    }
                )
            img_match = _re.search(r"\[Image description\]:?\s*(.+)", full_text, _re.DOTALL)
            if img_match:
                snippet = img_match.group(1).strip().split("\n")[0][:500]
                preprocess_samples.append(
                    {
                        "item_type": "media",
                        "agent": "image_describer",
                        "content": f"{snippet}…",
                        "model": provider.get_model_string("image_describer"),
                    }
                )
            img_meta = None
            if not img_match:
                img_meta = _re.search(r"\[Attachment:.*?\(image", full_text)
                if img_meta:
                    preprocess_samples.append(
                        {
                            "item_type": "media",
                            "agent": "image_describer",
                            "content": "Vision skipped (message text sufficient)",
                            "model": provider.get_model_string("image_describer"),
                            "status": "skipped",
                        }
                    )
            vid_match = _re.search(
                r"\[Video (?:summary|transcript|analysis)\]:?\s*(.+)", full_text, _re.DOTALL
            )
            if vid_match:
                snippet = vid_match.group(1).strip()[:2000]
                preprocess_samples.append(
                    {
                        "item_type": "media",
                        "agent": "video_analyzer",
                        "content": f"{snippet}…",
                        "model": provider.get_model_string("video_analyzer"),
                    }
                )
            vid_vis = _re.search(r"\[Video visual description\]:?\s*(.+)", full_text, _re.DOTALL)
            if vid_vis:
                snippet = vid_vis.group(1).strip()[:2000]
                preprocess_samples.append(
                    {
                        "item_type": "media",
                        "agent": "video_analyzer",
                        "content": f"{snippet}…",
                        "model": provider.get_model_string("video_analyzer"),
                    }
                )
            aud_match = _re.search(
                r"\[Audio (?:summary|transcript)\]:?\s*(.+)", full_text, _re.DOTALL
            )
            if aud_match:
                snippet = aud_match.group(1).strip()[:2000]
                preprocess_samples.append(
                    {
                        "item_type": "media",
                        "agent": "audio_transcriber",
                        "content": f"{snippet}…",
                        "model": provider.get_model_string("audio_transcriber"),
                    }
                )

            # Detect failed media agents (timeout or other errors)
            if m.get("modality") == "mixed":
                # img_meta covers [Attachment: ...] fallbacks when download failed but metadata was preserved
                has_media_output = bool(
                    doc_match or img_match or img_meta or vid_match or vid_vis or aud_match
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
                    agent = agent_map.get(media_type, "media_processor")
                    model_key = model_map.get(media_type, "document_digester")
                    media_names = m.get("source_media_names", [])
                    file_name = media_names[0] if media_names else "unknown"
                    is_timeout = "processing timed out" in full_text.lower()
                    preprocess_samples.append(
                        {
                            "item_type": "media",
                            "agent": agent,
                            "content": f"Processing timed out for {file_name}"
                            if is_timeout
                            else f"Processing failed for {file_name}",
                            "model": provider.get_model_string(model_key),
                            "status": "timeout" if is_timeout else "error",
                        }
                    )
            preprocess_samples.append(
                {
                    "item_type": "message",
                    "author": author,
                    "content": f"{first_line}{'…' if len(full_text.split(chr(10))[0]) > 200 else ''}",
                    "tags": badges,
                }
            )

        summary_parts = [f"Retained {len(preprocessed_messages)} messages"]
        if media_count:
            summary_parts.append(f"{media_count} media")

        activity_log.append(
            {
                "type": "stage_output",
                "agent": "preprocessor",
                "message": " · ".join(summary_parts),
                "metrics": {"messages": len(preprocessed_messages), "media": media_count},
                "samples": preprocess_samples[:20],
                "elapsed": stage_timings["preprocessor"],
            }
        )

        if not preprocessed_messages:
            logger.info(
                "batch_pipeline: no preprocessed messages job_id=%s batch=%d — skipping extraction",
                sync_job_id,
                batch_num,
            )
            breakdown.duration_seconds = stage_timings["preprocessor"]
            return breakdown

        # Serialize messages once for prompt injection
        preprocessed_json = json.dumps(preprocessed_messages, default=str)
        known_entities_json = json.dumps(known_entities, default=str)

        # ── Stage 2: Batch Extract ─────────────────────────────────────────
        fact_model = provider.get_model_string("fact_extractor")
        entity_model = provider.get_model_string("entity_extractor")

        stage2_label = "Step 2/6 — Extracting facts (Batch API: processing)"
        stage3_label = "Step 3/6 — Extracting entities (Batch API: processing)"

        activity_log.append(
            {
                "type": "stage_start",
                "agent": "fact_extractor",
                "stage": stage2_label,
                "model": fact_model,
            }
        )
        activity_log.append(
            {
                "type": "stage_start",
                "agent": "entity_extractor",
                "stage": stage3_label,
                "model": entity_model,
            }
        )
        await _update_progress(stage2_label)

        fact_prompt = _build_fact_prompt(
            channel_name=channel_name,
            preprocessed_messages_json=preprocessed_json,
            max_facts_per_message=_max_facts,
        )
        entity_prompt = _build_entity_prompt(
            channel_name=channel_name,
            channel_id=channel_id,
            known_entities_json=known_entities_json,
            preprocessed_messages_json=preprocessed_json,
        )

        fact_client = GeminiBatchClient(
            model=fact_model,
            api_key=settings.google_api_key,
            poll_interval=settings.batch_poll_interval_seconds,
            max_wait=settings.batch_max_wait_seconds,
        )
        entity_client = GeminiBatchClient(
            model=entity_model,
            api_key=settings.google_api_key,
            poll_interval=settings.batch_poll_interval_seconds,
            max_wait=settings.batch_max_wait_seconds,
        )

        t_extract = time.monotonic()

        # Submit both jobs concurrently
        fact_job_name, entity_job_name = await asyncio.gather(
            fact_client.submit_job(
                [BatchRequest(key="facts", prompt=fact_prompt)],
                display_name=f"facts-{sync_job_id}-batch{batch_num}",
            ),
            entity_client.submit_job(
                [BatchRequest(key="entities", prompt=entity_prompt)],
                display_name=f"entities-{sync_job_id}-batch{batch_num}",
            ),
        )

        # Poll both jobs concurrently
        fact_job, entity_job = await asyncio.gather(
            fact_client.poll_job(fact_job_name),
            entity_client.poll_job(entity_job_name),
        )

        extract_elapsed = round(time.monotonic() - t_extract, 2)
        stage_timings["fact_extractor"] = extract_elapsed
        stage_timings["entity_extractor"] = extract_elapsed

        # Parse fact response
        fact_responses = fact_client.parse_responses(fact_job, ["facts"])
        fact_text = fact_responses.get("facts", "")
        extracted_facts: dict[str, Any] = {}
        try:
            extracted_facts = json.loads(fact_text) if fact_text else {}
        except json.JSONDecodeError:
            logger.warning(
                "batch_pipeline: fact JSON parse failed job_id=%s batch=%d — attempting recovery",
                sync_job_id,
                batch_num,
            )
            recovered = recover_facts_from_truncated(fact_text)
            extracted_facts = recovered if recovered is not None else {"facts": []}

        # Parse entity response
        entity_responses = entity_client.parse_responses(entity_job, ["entities"])
        entity_text = entity_responses.get("entities", "")
        extracted_entities: dict[str, Any] = {}
        if not skip_entity_extraction:
            try:
                extracted_entities = json.loads(entity_text) if entity_text else {}
            except json.JSONDecodeError:
                logger.warning(
                    "batch_pipeline: entity JSON parse failed job_id=%s batch=%d — attempting recovery",
                    sync_job_id,
                    batch_num,
                )
                recovered_ent = recover_entities_from_truncated(entity_text)
                extracted_entities = (
                    recovered_ent
                    if recovered_ent is not None
                    else {"entities": [], "relationships": []}
                )
        else:
            extracted_entities = {"entities": [], "relationships": []}

        # Log extraction outputs
        facts_list: list[dict] = (
            extracted_facts.get("facts", []) if isinstance(extracted_facts, dict) else []
        )
        entities_list: list[dict] = (
            extracted_entities.get("entities", []) if isinstance(extracted_entities, dict) else []
        )
        rels_list: list[dict] = (
            extracted_entities.get("relationships", [])
            if isinstance(extracted_entities, dict)
            else []
        )

        avg_quality = (
            sum(f.get("quality_score", 0) for f in facts_list) / len(facts_list)
            if facts_list
            else 0.0
        )
        activity_log.append(
            {
                "type": "stage_output",
                "agent": "fact_extractor",
                "message": f"Extracted {len(facts_list)} facts (avg quality {avg_quality:.2f})",
                "model": fact_model,
                "metrics": {
                    "count": len(facts_list),
                    "avg_quality": float(f"{avg_quality:.2f}"),
                },
                "samples": [
                    {
                        "item_type": "fact",
                        "content": (f.get("memory_text") or "")[:300],
                        "score": f.get("quality_score", 0),
                        "tags": [f.get("importance", "?")],
                    }
                    for f in facts_list[:5]
                ],
                "elapsed": extract_elapsed,
            }
        )
        activity_log.append(
            {
                "type": "stage_output",
                "agent": "entity_extractor",
                "message": f"Found {len(entities_list)} entities, {len(rels_list)} relationships",
                "model": entity_model,
                "metrics": {
                    "entities": len(entities_list),
                    "relationships": len(rels_list),
                },
                "samples": [
                    {
                        "item_type": "entity",
                        "content": e.get("name", "?"),
                        "tags": [e.get("type", "?")],
                    }
                    for e in entities_list[:8]
                ]
                + [
                    {
                        "item_type": "relationship",
                        "source": r.get("source", "?"),
                        "rel_type": r.get("type", "?"),
                        "target": r.get("target", "?"),
                    }
                    for r in rels_list[:5]
                ],
                "elapsed": extract_elapsed,
            }
        )

        # ── Stage 3: Quality Gates ─────────────────────────────────────────
        extracted_facts = _apply_fact_quality_gate(extracted_facts, quality_threshold)
        # Bridge: classified_facts = extracted_facts (mirrors quality_gates callback)
        classified_facts = extracted_facts

        extracted_entities = _apply_entity_quality_gate(
            extracted_entities, settings.entity_threshold
        )

        # ── Stage 4: Enrich ────────────────────────────────────────────────
        stage4_label = _STAGE_LABELS["embedder"]
        activity_log.append(
            {
                "type": "stage_start",
                "agent": "embedder",
                "stage": stage4_label,
                "model": provider.embedding_model,
            }
        )
        await _update_progress(stage4_label)

        t_enrich = time.monotonic()
        enrich_state = await _run_adk_agent(
            ParallelAgent(
                name="batch_enrich",
                sub_agents=[
                    EmbedderAgent(name="embedder"),
                    create_cross_batch_validator(),
                ],
            ),
            state={
                "classified_facts": classified_facts,
                "extracted_entities": extracted_entities,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "batch_num": batch_num,
                "sync_job_id": sync_job_id,
                "known_entities": known_entities,
                "skip_entity_extraction": skip_entity_extraction,
                "skip_graph_writes": bool(ingestion_config and ingestion_config.skip_graph_writes),
            },
        )
        enrich_elapsed = round(time.monotonic() - t_enrich, 2)
        stage_timings["embedder"] = enrich_elapsed
        stage_timings["cross_batch_validator_agent"] = enrich_elapsed

        embedded_facts: list[dict] = enrich_state.get("embedded_facts") or []
        validated_entities: dict[str, Any] = enrich_state.get("validated_entities") or {}

        activity_log.append(
            {
                "type": "stage_output",
                "agent": "embedder",
                "message": f"Embedded {len(embedded_facts)} facts",
                "model": provider.embedding_model,
                "metrics": {"embedded": len(embedded_facts)},
                "elapsed": enrich_elapsed,
            }
        )

        validated_ents: list[dict] = (
            validated_entities.get("entities", []) if isinstance(validated_entities, dict) else []
        )
        validated_merges: list[dict] = (
            validated_entities.get("merges", []) if isinstance(validated_entities, dict) else []
        )
        activity_log.append(
            {
                "type": "stage_output",
                "agent": "cross_batch_validator_agent",
                "message": (
                    f"Validated {len(validated_ents)} entities"
                    + (f", {len(validated_merges)} merges" if validated_merges else "")
                ),
                "model": provider.get_model_string("cross_batch_validator_agent"),
                "metrics": {
                    "entities": len(validated_ents),
                    "merges": len(validated_merges),
                },
                "samples": [
                    {
                        "item_type": "validation",
                        "content": (
                            f"{', '.join(mg['merged_from']) if isinstance(mg.get('merged_from'), list) else mg.get('merged_from', '?')}"
                            f" → {mg.get('canonical', '?')}"
                        ),
                    }
                    for mg in validated_merges[:5]
                ]
                or None,
                "elapsed": enrich_elapsed,
            }
        )
        await _update_progress(_STAGE_LABELS["cross_batch_validator_agent"])

        # ── Stage 5: Persist ───────────────────────────────────────────────
        stage6_label = _STAGE_LABELS["persister"]
        activity_log.append(
            {
                "type": "stage_start",
                "agent": "persister",
                "stage": stage6_label,
                "model": None,
            }
        )
        await _update_progress(stage6_label)

        t_persist = time.monotonic()
        persist_state = await _run_adk_agent(
            SequentialAgent(
                name="batch_persister",
                sub_agents=[PersisterAgent(name="persister")],
            ),
            state={
                "embedded_facts": embedded_facts,
                "validated_entities": validated_entities,
                "channel_id": channel_id,
                "channel_name": channel_name,
                "batch_num": batch_num,
                "sync_job_id": sync_job_id,
                "skip_graph_writes": bool(ingestion_config and ingestion_config.skip_graph_writes),
            },
        )
        stage_timings["persister"] = round(time.monotonic() - t_persist, 2)

        persist_result: dict[str, Any] = persist_state.get("persist_result") or {}
        wv_count = len(persist_result.get("weaviate_ids") or [])
        neo_count = persist_result.get("entity_count") or 0
        rel_count = persist_result.get("relationship_count") or 0

        activity_log.append(
            {
                "type": "stage_output",
                "agent": "persister",
                "message": (
                    f"Saved {wv_count} facts → Weaviate, "
                    f"{neo_count} entities + {rel_count} rels → Neo4j"
                ),
                "metrics": {
                    "weaviate_facts": wv_count,
                    "neo4j_entities": neo_count,
                    "neo4j_rels": rel_count,
                },
                "elapsed": stage_timings["persister"],
            }
        )

        # ── Final progress flush ───────────────────────────────────────────
        await stores.mongodb.update_sync_progress(
            job_id=sync_job_id,
            processed=0,
            current_batch=batch_num,
            total_batches=max_batches,
            current_stage=f"Step 7/7 — Batch {batch_num} complete",
            stage_timings=stage_timings,
            stage_details={"activity_log": activity_log[-50:]},
        )

        # ── Populate breakdown ─────────────────────────────────────────────
        filtered_facts_list: list[dict] = (
            extracted_facts.get("facts", []) if isinstance(extracted_facts, dict) else []
        )
        breakdown.facts_count = wv_count
        breakdown.entities_count = neo_count
        breakdown.relationships_count = rel_count
        breakdown.facts_stored = wv_count
        breakdown.sample_facts = [f.get("memory_text", "")[:120] for f in filtered_facts_list[:3]]
        breakdown.sample_entities = [
            {"name": e.get("name", ""), "type": e.get("type", "")} for e in validated_ents[:3]
        ]
        breakdown.duration_seconds = round(sum(stage_timings.values()), 2)

        logger.info(
            "batch_pipeline: batch %d/%d complete job_id=%s facts=%d entities=%d rels=%d",
            batch_num,
            max_batches,
            sync_job_id,
            wv_count,
            neo_count,
            rel_count,
        )
        return breakdown

    async def process_batch_with_retry(
        self,
        messages: list[dict],
        channel_id: str,
        channel_name: str,
        sync_job_id: str,
        batch_num: int,
        max_batches: int,
        known_entities: list[dict],
        ingestion_config: IngestionConfig | None = None,
        max_retries: int = 3,
    ) -> BatchBreakdown:
        """Process one batch with progressive retry on failure.

        Attempt 1 (normal): Call process_batch with original params.
        Attempt 2: Reduce max_facts_per_message to 1 and retry.
        Attempt 3: Split messages in half, process each half, merge results.
        Attempt 4: Return a failed BatchBreakdown with the error.
        """
        # Attempt 1: normal
        logger.info(
            "batch_pipeline: attempt 1/%d job_id=%s batch=%d",
            max_retries + 1,
            sync_job_id,
            batch_num,
        )
        try:
            return await self.process_batch(
                messages=messages,
                channel_id=channel_id,
                channel_name=channel_name,
                sync_job_id=sync_job_id,
                batch_num=batch_num,
                max_batches=max_batches,
                known_entities=known_entities,
                ingestion_config=ingestion_config,
            )
        except Exception as exc1:
            logger.warning(
                "batch_pipeline: attempt 1 failed job_id=%s batch=%d: %s",
                sync_job_id,
                batch_num,
                exc1,
            )

        if max_retries < 1:
            breakdown = BatchBreakdown(batch_num=batch_num)
            breakdown.error = "attempt 1 failed, no retries configured"
            return breakdown

        # Attempt 2: reduce max_facts_per_message to 1
        logger.info(
            "batch_pipeline: attempt 2/%d (reduced facts) job_id=%s batch=%d",
            max_retries + 1,
            sync_job_id,
            batch_num,
        )
        try:
            from beever_atlas.infra.config import get_settings as _get_settings

            if ingestion_config is not None:
                reduced_config = ingestion_config.model_copy(update={"max_facts_per_message": 1})
            else:
                _get_settings()
                reduced_config = IngestionConfig(max_facts_per_message=1)

            return await self.process_batch(
                messages=messages,
                channel_id=channel_id,
                channel_name=channel_name,
                sync_job_id=sync_job_id,
                batch_num=batch_num,
                max_batches=max_batches,
                known_entities=known_entities,
                ingestion_config=reduced_config,
            )
        except Exception as exc2:
            logger.warning(
                "batch_pipeline: attempt 2 failed job_id=%s batch=%d: %s",
                sync_job_id,
                batch_num,
                exc2,
            )

        if max_retries < 2:
            breakdown = BatchBreakdown(batch_num=batch_num)
            breakdown.error = "attempts 1-2 failed, max_retries=1"
            return breakdown

        # Attempt 3: split messages in half, process each half, merge
        logger.info(
            "batch_pipeline: attempt 3/%d (split half) job_id=%s batch=%d",
            max_retries + 1,
            sync_job_id,
            batch_num,
        )
        try:
            mid = len(messages) // 2
            first_half = messages[:mid] if mid > 0 else messages
            second_half = messages[mid:] if mid > 0 and mid < len(messages) else []

            breakdown_a = await self.process_batch(
                messages=first_half,
                channel_id=channel_id,
                channel_name=channel_name,
                sync_job_id=sync_job_id,
                batch_num=batch_num,
                max_batches=max_batches,
                known_entities=known_entities,
                ingestion_config=ingestion_config,
            )

            if second_half:
                breakdown_b = await self.process_batch(
                    messages=second_half,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    sync_job_id=sync_job_id,
                    batch_num=batch_num,
                    max_batches=max_batches,
                    known_entities=known_entities,
                    ingestion_config=ingestion_config,
                )
                # Merge the two halves
                merged = BatchBreakdown(batch_num=batch_num)
                merged.facts_count = breakdown_a.facts_count + breakdown_b.facts_count
                merged.entities_count = breakdown_a.entities_count + breakdown_b.entities_count
                merged.relationships_count = (
                    breakdown_a.relationships_count + breakdown_b.relationships_count
                )
                merged.facts_stored = breakdown_a.facts_stored + breakdown_b.facts_stored
                merged.sample_facts = (breakdown_a.sample_facts + breakdown_b.sample_facts)[:3]
                merged.sample_entities = (
                    breakdown_a.sample_entities + breakdown_b.sample_entities
                )[:3]
                merged.duration_seconds = (breakdown_a.duration_seconds or 0.0) + (
                    breakdown_b.duration_seconds or 0.0
                )
                return merged

            return breakdown_a
        except Exception as exc3:
            logger.warning(
                "batch_pipeline: attempt 3 failed job_id=%s batch=%d: %s",
                sync_job_id,
                batch_num,
                exc3,
            )

        # All attempts exhausted — return failed breakdown
        logger.error(
            "batch_pipeline: all retry attempts failed job_id=%s batch=%d",
            sync_job_id,
            batch_num,
        )
        breakdown = BatchBreakdown(batch_num=batch_num)
        breakdown.error = f"all {max_retries + 1} attempts failed"
        return breakdown
