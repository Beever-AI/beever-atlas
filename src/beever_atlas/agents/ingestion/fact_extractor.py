"""Fact extraction agent — Stage 2 of the ingestion pipeline."""
from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.fact_extractor import FACT_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.callbacks.quality_gates import fact_extraction_with_recovery
from beever_atlas.agents.callbacks.checkpoint_skip import make_checkpoint_skip_callback
from beever_atlas.agents.schemas.extraction import FactExtractionResult
from beever_atlas.infra.config import get_settings
from beever_atlas.llm import get_llm_provider


def create_fact_extractor(model=None) -> LlmAgent:
    """Create the fact extraction LlmAgent."""
    agent_kwargs: dict = {
        "name": "fact_extractor",
        "model": model or get_llm_provider().resolve_model("fact_extractor"),
        "instruction": FACT_EXTRACTOR_INSTRUCTION,
        "output_key": "extracted_facts",
        "generate_content_config": types.GenerateContentConfig(
            response_mime_type="application/json",
            max_output_tokens=131072,
        ),
        "before_agent_callback": make_checkpoint_skip_callback("fact_extractor"),
        "after_agent_callback": fact_extraction_with_recovery,
    }
    if get_settings().use_llm_structured_output:
        # Schema-constrained decoding via ADK's output_schema. Gemini cannot
        # emit JSON that violates the schema. Trade-off: output_schema disables
        # tool use for this agent — fact_extractor has no tools, so this is safe.
        agent_kwargs["output_schema"] = FactExtractionResult
    return LlmAgent(**agent_kwargs)
