"""Contradiction detection agent — detects fact contradictions."""

from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.contradiction_detector import CONTRADICTION_DETECTOR_INSTRUCTION
from beever_atlas.agents.schemas.validation import ContradictionReport
from beever_atlas.llm import get_llm_provider
from beever_atlas.services.adk_recovery import wrap_with_recovery
from beever_atlas.services.json_recovery import recover_truncated_json


def _recover_contradiction(text: str) -> dict | None:
    result = recover_truncated_json(text)
    if isinstance(result, dict):
        return result
    return {"contradictions": []}


def create_contradiction_detector(model=None) -> LlmAgent:
    """Create the contradiction detection LlmAgent."""
    agent = LlmAgent(
        name="contradiction_detector",
        model=model or get_llm_provider().resolve_model("contradiction_detector"),
        instruction=CONTRADICTION_DETECTOR_INSTRUCTION,
        output_key="contradiction_report",
        output_schema=ContradictionReport,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    return wrap_with_recovery(agent, _recover_contradiction, ContradictionReport)
