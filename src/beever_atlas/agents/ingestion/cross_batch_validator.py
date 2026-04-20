"""Cross-batch validator agent — Stage 5 of the ingestion pipeline."""

from __future__ import annotations

import logging

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types

from beever_atlas.agents.prompts.cross_batch_validator import CROSS_BATCH_VALIDATOR_INSTRUCTION
from beever_atlas.agents.schemas.validation import ValidationResult
from beever_atlas.agents.callbacks.checkpoint_skip import make_checkpoint_skip_callback
from beever_atlas.llm import get_llm_provider
from beever_atlas.services.adk_recovery import wrap_with_recovery
from beever_atlas.services.json_recovery import recover_validation_from_truncated

logger = logging.getLogger(__name__)

_checkpoint_skip = make_checkpoint_skip_callback("cross_batch_validator_agent")


def _skip_if_no_work(callback_context: CallbackContext) -> types.Content | None:
    """Skip validator if checkpointed OR if no entities to validate."""
    # First check: checkpoint skip
    checkpoint_result = _checkpoint_skip(callback_context)
    if checkpoint_result is not None:
        return checkpoint_result

    # Second check: empty entities
    raw = callback_context.state.get("extracted_entities")
    if not isinstance(raw, dict):
        if raw is not None:
            logger.warning(
                "CrossBatchValidator: extracted_entities is %s, not dict; treating as empty",
                type(raw).__name__,
            )
        callback_context.state["validated_entities"] = {"entities": [], "relationships": []}
        return types.Content(
            role="model",
            parts=[types.Part(text="[Skipped: no entities to validate]")],
        )
    entities = raw.get("entities") or []
    relationships = raw.get("relationships") or []
    if not entities and not relationships:
        logger.info("CrossBatchValidator: skipping — 0 entities, 0 relationships")
        # Write empty validation result so downstream stages have the key
        callback_context.state["validated_entities"] = {"entities": [], "relationships": []}
        return types.Content(
            role="model",
            parts=[types.Part(text="[Skipped: no entities to validate]")],
        )
    return None


def create_cross_batch_validator(model=None) -> LlmAgent:
    """Create the cross-batch validator LlmAgent."""
    agent = LlmAgent(
        name="cross_batch_validator_agent",
        model=model or get_llm_provider().resolve_model("cross_batch_validator"),
        instruction=CROSS_BATCH_VALIDATOR_INSTRUCTION,
        output_schema=ValidationResult,
        output_key="validated_entities",
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=63000,
        ),
        before_agent_callback=_skip_if_no_work,
    )
    return wrap_with_recovery(agent, recover_validation_from_truncated, ValidationResult)
