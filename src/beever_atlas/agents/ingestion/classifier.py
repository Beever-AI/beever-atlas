"""Classifier agent — Stage 4 of the ingestion pipeline."""
from __future__ import annotations

from google.adk.agents import LlmAgent

from beever_atlas.agents.prompts.classifier import CLASSIFIER_INSTRUCTION
from beever_atlas.agents.schemas.classification import ClassificationResult
from beever_atlas.llm import get_llm_provider


def create_classifier() -> LlmAgent:
    """Create the classifier LlmAgent."""
    return LlmAgent(
        name="classifier_agent",
        model=get_llm_provider().fast,
        instruction=CLASSIFIER_INSTRUCTION,
        output_schema=ClassificationResult,
        output_key="classified_facts",
    )
