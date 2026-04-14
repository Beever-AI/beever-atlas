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
from beever_atlas.services.adk_recovery import wrap_with_recovery
from beever_atlas.services.json_recovery import recover_truncated_json


def _recover_summary(text: str) -> dict | None:
    result = recover_truncated_json(text)
    if isinstance(result, dict):
        return result
    return {"summary_text": ""}


def _recover_topic_summary(text: str) -> dict | None:
    result = recover_truncated_json(text)
    if isinstance(result, dict):
        return result
    return {"summary_text": ""}


def _recover_channel_summary(text: str) -> dict | None:
    result = recover_truncated_json(text)
    if isinstance(result, dict):
        return result
    return {"summary_text": ""}


def create_summarizer(instruction: str, model=None) -> LlmAgent:
    """Create a legacy summarizer LlmAgent with flat SummaryResult output.

    Args:
        instruction: The prompt template with context already interpolated.
        model: Optional model override. If None, resolved from config.
    """
    agent = LlmAgent(
        name="summarizer",
        model=model or get_llm_provider().resolve_model("summarizer"),
        instruction=instruction,
        output_key="summary_result",
        output_schema=SummaryResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    return wrap_with_recovery(agent, _recover_summary, SummaryResult)


def create_topic_summarizer(instruction: str, model=None) -> LlmAgent:
    """Create a topic summarizer with structured TopicSummaryResult output.

    Returns title, multi-angle summaries, focused topic_tags, and FAQ candidates.
    """
    agent = LlmAgent(
        name="topic_summarizer",
        model=model or get_llm_provider().resolve_model("summarizer"),
        instruction=instruction,
        output_key="summary_result",
        output_schema=TopicSummaryResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    return wrap_with_recovery(agent, _recover_topic_summary, TopicSummaryResult)


def create_channel_summarizer(instruction: str, model=None) -> LlmAgent:
    """Create a channel summarizer with structured ChannelSummaryResult output.

    Returns multi-angle summaries, description, themes, momentum,
    team_dynamics, and glossary terms.
    """
    agent = LlmAgent(
        name="channel_summarizer",
        model=model or get_llm_provider().resolve_model("summarizer"),
        instruction=instruction,
        output_key="summary_result",
        output_schema=ChannelSummaryResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    return wrap_with_recovery(agent, _recover_channel_summary, ChannelSummaryResult)
