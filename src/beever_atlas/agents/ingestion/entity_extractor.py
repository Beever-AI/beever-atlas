"""Entity extraction agent — Stage 3 of the ingestion pipeline."""
from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.entity_extractor import ENTITY_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.callbacks.quality_gates import entity_extraction_with_recovery
from beever_atlas.agents.callbacks.checkpoint_skip import make_checkpoint_skip_callback
from beever_atlas.agents.schemas.extraction import EntityExtractionResult
from beever_atlas.infra.config import get_settings
from beever_atlas.llm import get_llm_provider


def create_entity_extractor(model=None) -> LlmAgent:
    """Create the entity extraction LlmAgent."""
    agent_kwargs: dict = {
        "name": "entity_extractor",
        "model": model or get_llm_provider().resolve_model("entity_extractor"),
        "instruction": ENTITY_EXTRACTOR_INSTRUCTION,
        "output_key": "extracted_entities",
        "generate_content_config": types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=65536,
        ),
        "before_agent_callback": make_checkpoint_skip_callback("entity_extractor"),
        "after_agent_callback": entity_extraction_with_recovery,
    }
    if get_settings().use_llm_structured_output:
        # output_schema: schema-constrained decoding; disables tool use (none here).
        agent_kwargs["output_schema"] = EntityExtractionResult
    return LlmAgent(**agent_kwargs)
