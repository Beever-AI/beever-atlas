"""Cross-batch validator agent — Stage 6 of the ingestion pipeline."""
from __future__ import annotations

from google.adk.agents import LlmAgent

from beever_atlas.agents.prompts.cross_batch_validator import CROSS_BATCH_VALIDATOR_INSTRUCTION
from beever_atlas.agents.schemas.validation import ValidationResult
from beever_atlas.llm import get_llm_provider


def create_cross_batch_validator() -> LlmAgent:
    """Create the cross-batch validator LlmAgent."""
    return LlmAgent(
        name="cross_batch_validator_agent",
        model=get_llm_provider().quality,
        instruction=CROSS_BATCH_VALIDATOR_INSTRUCTION,
        output_schema=ValidationResult,
        output_key="validated_entities",
    )
