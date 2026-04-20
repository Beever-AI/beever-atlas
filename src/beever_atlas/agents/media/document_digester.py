"""Document digester agent — digests long documents into a concise Markdown summary."""

from __future__ import annotations

from google.adk.agents import LlmAgent

from beever_atlas.agents.prompts.media import DOCUMENT_DIGESTER_INSTRUCTION
from beever_atlas.llm import get_llm_provider


def create_document_digester(model=None) -> LlmAgent:
    """Create the document digester LlmAgent."""
    return LlmAgent(
        name="document_digester",
        model=model or get_llm_provider().resolve_model("document_digester"),
        instruction=DOCUMENT_DIGESTER_INSTRUCTION,
        output_key="document_digest",
        # Output is arbitrary markdown string, so we don't define a strict JSON schema
    )
