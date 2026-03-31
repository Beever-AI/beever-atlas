"""Fact extraction agent — Stage 2 of the ingestion pipeline."""
from __future__ import annotations

from google.adk.agents import LlmAgent

from beever_atlas.agents.prompts.fact_extractor import FACT_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.schemas.extraction import FactExtractionResult
from beever_atlas.agents.callbacks.quality_gates import fact_quality_gate_callback
from beever_atlas.llm import get_llm_provider


def create_fact_extractor() -> LlmAgent:
    """Create the fact extraction LlmAgent."""
    return LlmAgent(
        name="fact_extractor",
        model=get_llm_provider().fast,
        instruction=FACT_EXTRACTOR_INSTRUCTION,
        output_key="extracted_facts",
        output_schema=FactExtractionResult,
        after_agent_callback=fact_quality_gate_callback,
    )
