from __future__ import annotations

import logging
from typing import Any

from google.adk.agents.callback_context import CallbackContext

from beever_atlas.agents.schemas.extraction import EntityExtractionResult, FactExtractionResult
from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)


def fact_quality_gate_callback(callback_context: CallbackContext) -> None:
    """Filter extracted facts below the configured quality threshold.

    This ``after_agent_callback`` reads ``extracted_facts`` from session state
    (written by the LlmAgent via ``output_key``), removes any facts whose
    ``quality_score`` is below ``settings.quality_threshold``, and writes the
    filtered list back.
    """
    # Per-channel quality_threshold from policy (via session state), else global default
    settings = get_settings()
    state_threshold = callback_context.state.get("quality_threshold")
    threshold: float = (
        state_threshold if state_threshold is not None else settings.quality_threshold
    )

    raw: Any = callback_context.state.get("extracted_facts")
    if raw is None:
        logger.warning("fact_quality_gate_callback: 'extracted_facts' not found in state.")
        return

    # ``output_key`` stores the Pydantic model as a dict (serialised by ADK).
    if isinstance(raw, dict):
        facts_dicts: list[dict[str, Any]] = raw.get("facts") or []
    elif isinstance(raw, FactExtractionResult):
        facts_dicts = [f.model_dump() for f in raw.facts]
    else:
        logger.warning(
            "fact_quality_gate_callback: unexpected type for extracted_facts: %s", type(raw)
        )
        return

    before_count = len(facts_dicts)
    filtered = [f for f in facts_dicts if f.get("quality_score", 0.0) >= threshold]
    after_count = len(filtered)

    if before_count != after_count:
        logger.info(
            "fact_quality_gate_callback: dropped %d/%d facts below threshold %.2f.",
            before_count - after_count,
            before_count,
            threshold,
        )

    # Write back as a plain dict so downstream stages can read it uniformly.
    if isinstance(raw, dict):
        callback_context.state["extracted_facts"] = {
            **raw,
            "facts": filtered,
        }
    else:
        callback_context.state["extracted_facts"] = {
            "facts": filtered,
            "skip_reason": raw.skip_reason if isinstance(raw, FactExtractionResult) else None,
        }

    # Bridge: also write as classified_facts so the embedder can read it
    # without the classifier stage (which has been removed).
    callback_context.state["classified_facts"] = callback_context.state["extracted_facts"]


def entity_quality_gate_callback(callback_context: CallbackContext) -> None:
    """Filter entities and relationships below the configured confidence threshold.

    This ``after_agent_callback`` reads ``extracted_entities`` from session
    state (written by the LlmAgent via ``output_key``), removes entities with
    no relationships above threshold, and removes relationships whose
    ``confidence`` is below ``settings.entity_threshold``.
    """
    settings = get_settings()
    threshold: float = settings.entity_threshold

    raw: Any = callback_context.state.get("extracted_entities")
    if raw is None:
        logger.warning("entity_quality_gate_callback: 'extracted_entities' not found in state.")
        return

    if isinstance(raw, dict):
        entities_dicts: list[dict[str, Any]] = raw.get("entities") or []
        rels_dicts: list[dict[str, Any]] = raw.get("relationships") or []
        skip_reason: str | None = raw.get("skip_reason")
    elif isinstance(raw, EntityExtractionResult):
        entities_dicts = [e.model_dump() for e in raw.entities]
        rels_dicts = [r.model_dump() for r in raw.relationships]
        skip_reason = raw.skip_reason
    else:
        logger.warning(
            "entity_quality_gate_callback: unexpected type for extracted_entities: %s",
            type(raw),
        )
        return

    rels_before = len(rels_dicts)
    filtered_rels = [r for r in rels_dicts if r.get("confidence", 0.0) >= threshold]
    rels_after = len(filtered_rels)

    if rels_before != rels_after:
        logger.info(
            "entity_quality_gate_callback: dropped %d/%d relationships below "
            "confidence threshold %.2f.",
            rels_before - rels_after,
            rels_before,
            threshold,
        )

    # Keep entities that are referenced in surviving relationships OR have
    # enough intrinsic value regardless of relationships (scope=global always kept).
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
            "entity_quality_gate_callback: dropped %d/%d channel-scoped entities "
            "with no qualifying relationships.",
            entities_before - entities_after,
            entities_before,
        )

    callback_context.state["extracted_entities"] = {
        "entities": filtered_entities,
        "relationships": filtered_rels,
        "skip_reason": skip_reason,
    }


def fact_extraction_with_recovery(callback_context: CallbackContext) -> None:
    """After-agent callback that attempts JSON recovery on malformed extraction output."""
    raw = callback_context.state.get("extracted_facts")

    # If extraction produced valid structured output, just run normal quality gate
    if isinstance(raw, dict) and "facts" in raw:
        return fact_quality_gate_callback(callback_context)

    # If raw is a string (LLM returned text instead of parsed JSON), try recovery
    if isinstance(raw, str):
        from beever_atlas.services.json_recovery import (
            recover_truncated_json_with_report,
        )

        result, report = recover_truncated_json_with_report(raw)
        recovered = None
        if isinstance(result, dict) and "facts" in result:
            recovered = result
        logger.warning(
            "truncation_report agent=fact_extractor recovered=%d lost_estimate=%d "
            "raw_bytes=%d last_boundary_offset=%d batch_idx=%s",
            report.recovered_count,
            report.estimated_lost,
            report.raw_bytes,
            report.last_boundary_offset,
            callback_context.state.get("batch_num", "?"),
        )
        if recovered:
            logger.info(
                "fact_extraction_with_recovery: recovered %d facts from truncated output",
                len(recovered["facts"]),
            )
            callback_context.state["extracted_facts"] = recovered
            return fact_quality_gate_callback(callback_context)

    # Fallback: set empty facts
    logger.warning("fact_extraction_with_recovery: no recoverable facts, setting empty")
    callback_context.state["extracted_facts"] = {"facts": [], "skip_reason": "extraction_failed"}
    callback_context.state["classified_facts"] = callback_context.state["extracted_facts"]


def entity_extraction_with_recovery(callback_context: CallbackContext) -> None:
    """After-agent callback that attempts JSON recovery on malformed entity extraction output."""
    raw = callback_context.state.get("extracted_entities")

    if isinstance(raw, dict) and ("entities" in raw or "relationships" in raw):
        return entity_quality_gate_callback(callback_context)

    if isinstance(raw, str):
        from beever_atlas.services.json_recovery import recover_truncated_json_with_report

        result, report = recover_truncated_json_with_report(raw)
        recovered = (
            result
            if isinstance(result, dict) and ("entities" in result or "relationships" in result)
            else None
        )
        logger.warning(
            "truncation_report agent=entity_extractor recovered=%d lost_estimate=%d "
            "raw_bytes=%d last_boundary_offset=%d batch_idx=%s",
            report.recovered_count,
            report.estimated_lost,
            report.raw_bytes,
            report.last_boundary_offset,
            callback_context.state.get("batch_num", "?"),
        )
        if recovered:
            logger.info(
                "entity_extraction_with_recovery: recovered %d entities from truncated output",
                len(recovered.get("entities", [])),
            )
            callback_context.state["extracted_entities"] = recovered
            return entity_quality_gate_callback(callback_context)

    logger.warning("entity_extraction_with_recovery: no recoverable entities, setting empty")
    callback_context.state["extracted_entities"] = {"entities": [], "relationships": []}
