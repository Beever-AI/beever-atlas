"""Coreference resolution agent — resolves pronouns before extraction."""
from __future__ import annotations

from google.adk.agents import LlmAgent
from google.genai import types

from beever_atlas.agents.prompts.coreference_resolver import COREFERENCE_RESOLVER_INSTRUCTION
from beever_atlas.agents.schemas.coreference import CoreferenceResult
from beever_atlas.llm import get_llm_provider
from beever_atlas.services.adk_recovery import wrap_with_recovery
from beever_atlas.services.json_recovery import recover_truncated_json


def _recover_coreference(text: str) -> dict | None:
    result = recover_truncated_json(text)
    if isinstance(result, dict):
        return result
    return {"resolved_messages": []}


def create_coreference_resolver(model=None) -> LlmAgent:
    """Create the coreference resolution LlmAgent."""
    agent = LlmAgent(
        name="coreference_resolver",
        model=model or get_llm_provider().resolve_model("coreference_resolver"),
        instruction=COREFERENCE_RESOLVER_INSTRUCTION,
        output_key="resolved_messages",
        output_schema=CoreferenceResult,
        generate_content_config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    return wrap_with_recovery(agent, _recover_coreference, CoreferenceResult)
