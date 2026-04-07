"""Consolidation summarizer agents — generate topic and channel summaries."""
from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.schemas.consolidation import (
    ChannelSummaryResult,
    SummaryResult,
    TopicSummaryResult,
)
from beever_atlas.llm import get_llm_provider


def create_summarizer(instruction: str, model=None) -> LlmAgent:
    """Create a legacy summarizer LlmAgent with flat SummaryResult output.

    Args:
        instruction: The prompt template with context already interpolated.
        model: Optional model override. If None, resolved from config.
    """
    return LlmAgent(
        name="summarizer",
        model=model or get_llm_provider().resolve_model("summarizer"),
        instruction=instruction,
        output_key="summary_result",
        output_schema=SummaryResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )


def create_topic_summarizer(instruction: str, model=None) -> LlmAgent:
    """Create a topic summarizer with structured TopicSummaryResult output.

    Returns title, multi-angle summaries, focused topic_tags, and FAQ candidates.
    """
    return LlmAgent(
        name="topic_summarizer",
        model=model or get_llm_provider().resolve_model("summarizer"),
        instruction=instruction,
        output_key="summary_result",
        output_schema=TopicSummaryResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )


def create_channel_summarizer(instruction: str, model=None) -> LlmAgent:
    """Create a channel summarizer with structured ChannelSummaryResult output.

    Returns multi-angle summaries, description, themes, momentum,
    team_dynamics, and glossary terms.
    """
    return LlmAgent(
        name="channel_summarizer",
        model=model or get_llm_provider().resolve_model("summarizer"),
        instruction=instruction,
        output_key="summary_result",
        output_schema=ChannelSummaryResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
