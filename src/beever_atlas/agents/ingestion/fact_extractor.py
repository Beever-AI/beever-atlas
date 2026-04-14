"""Fact extraction agent — Stage 2 of the ingestion pipeline."""
from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.fact_extractor import FACT_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.callbacks.quality_gates import fact_extraction_with_recovery
from beever_atlas.agents.callbacks.checkpoint_skip import make_checkpoint_skip_callback
from beever_atlas.llm import get_llm_provider


def create_fact_extractor(model=None) -> LlmAgent:
    """Create the fact extraction LlmAgent.

    See ``entity_extractor.create_entity_extractor`` for the full rationale.
    Schema-constrained decoding is intentionally disabled because ADK's
    ``output_schema`` raises before the recovery callback runs, and ADK
    refuses ``response_schema`` on ``GenerateContentConfig``. Safety chain:
    output-aware batching → recovery callback → retry ladder.
    """
    agent_kwargs: dict = {
        "name": "fact_extractor",
        "model": model or get_llm_provider().resolve_model("fact_extractor"),
        "instruction": FACT_EXTRACTOR_INSTRUCTION,
        "output_key": "extracted_facts",
        "generate_content_config": types.GenerateContentConfig(
            response_mime_type="application/json",
            # Gemini 2.5 Flash real output ceiling ~65k; prior 131072 exceeded model limit causing silent truncation (see .omc/plans/ingestion-pipeline-hardening.md).
            max_output_tokens=63000,
        ),
        "before_agent_callback": make_checkpoint_skip_callback("fact_extractor"),
        "after_agent_callback": fact_extraction_with_recovery,
    }
    return LlmAgent(**agent_kwargs)
