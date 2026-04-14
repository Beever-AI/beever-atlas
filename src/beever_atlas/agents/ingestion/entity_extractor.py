"""Entity extraction agent — Stage 3 of the ingestion pipeline."""
from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.entity_extractor import ENTITY_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.callbacks.quality_gates import entity_extraction_with_recovery
from beever_atlas.agents.callbacks.checkpoint_skip import make_checkpoint_skip_callback
from beever_atlas.llm import get_llm_provider


def create_entity_extractor(model=None) -> LlmAgent:
    """Create the entity extraction LlmAgent.

    Schema-constrained decoding is intentionally NOT enabled here. ADK's
    `LlmAgent.output_schema` calls `model_validate_json` on the raw response
    and raises `ValidationError` *before* `after_agent_callback` runs, which
    bypasses `entity_extraction_with_recovery` and crashes the batch on
    truncation. ADK also refuses `response_schema` on `GenerateContentConfig`
    (forces use of output_schema), so there is no soft-enforcement path.

    The EOF safety chain is instead:
      1. Output-aware batching (`BATCH_MAX_OUTPUT_TOKENS`) prevents truncation.
      2. `entity_extraction_with_recovery` salvages partial JSON when it happens.
      3. Retry ladder (reduce max_facts → halve batch) catches the rest.
    """
    agent_kwargs: dict = {
        "name": "entity_extractor",
        "model": model or get_llm_provider().resolve_model("entity_extractor"),
        "instruction": ENTITY_EXTRACTOR_INSTRUCTION,
        "output_key": "extracted_entities",
        "generate_content_config": types.GenerateContentConfig(
            response_mime_type="application/json",
            # Gemini 2.5 Flash real output ceiling ~65k; prior 131072 exceeded model limit causing silent truncation (see .omc/plans/ingestion-pipeline-hardening.md).
            max_output_tokens=65536,
        ),
        "before_agent_callback": make_checkpoint_skip_callback("entity_extractor"),
        "after_agent_callback": entity_extraction_with_recovery,
    }
    return LlmAgent(**agent_kwargs)
