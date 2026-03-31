"""Entity extraction agent — Stage 3 of the ingestion pipeline."""
from __future__ import annotations

from google.adk.agents import LlmAgent

from beever_atlas.agents.prompts.entity_extractor import ENTITY_EXTRACTOR_INSTRUCTION
from beever_atlas.agents.schemas.extraction import EntityExtractionResult
from beever_atlas.agents.callbacks.quality_gates import entity_quality_gate_callback
from beever_atlas.llm import get_llm_provider


def create_entity_extractor() -> LlmAgent:
    """Create the entity extraction LlmAgent."""
    return LlmAgent(
        name="entity_extractor",
        model=get_llm_provider().fast,
        instruction=ENTITY_EXTRACTOR_INSTRUCTION,
        output_key="extracted_entities",
        output_schema=EntityExtractionResult,
        after_agent_callback=entity_quality_gate_callback,
    )
