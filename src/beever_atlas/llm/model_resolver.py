"""Model resolution — maps model strings to ADK-compatible model objects."""

from __future__ import annotations

import logging
import os
from typing import Any

from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)

# All known agent names in the system
AGENT_NAMES: list[str] = [
    "fact_extractor",
    "entity_extractor",
    "cross_batch_validator",
    "coreference_resolver",
    "contradiction_detector",
    "image_describer",
    "video_analyzer",
    "audio_transcriber",
    "summarizer",
    "document_digester",
    "echo",
    "wiki_compiler",
    "wiki_maintainer",
    "qa_agent",
    "qa_router",
    "csv_mapper",
]

# Default model assignments — Flash for complex, Lite for simple, Gemma 4 E4B for media
DEFAULT_AGENT_MODELS: dict[str, str] = {
    "fact_extractor": "gemini-2.5-flash",
    "entity_extractor": "gemini-2.5-flash",
    "cross_batch_validator": "gemini-2.5-flash-lite",
    "coreference_resolver": "gemini-2.5-flash-lite",
    "contradiction_detector": "gemini-2.5-flash-lite",
    "summarizer": "gemini-2.5-flash-lite",
    "document_digester": "ollama_chat/gemma4:e4b",
    "echo": "gemini-2.5-flash-lite",
    "image_describer": "ollama_chat/gemma4:e4b",
    "video_analyzer": "gemini-2.5-flash-lite",
    "audio_transcriber": "gemini-2.5-flash-lite",
    "wiki_compiler": "gemini-2.5-flash",
    "wiki_maintainer": "gemini-2.5-flash",
    "qa_router": "gemini-2.5-flash-lite",
    "qa_agent": "gemini-2.5-flash",
    "csv_mapper": "gemini-2.5-flash-lite",
}

# Known Gemini models available via Google AI API
KNOWN_GEMINI_MODELS: list[str] = [
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

# Known Ollama models (user may have others; these are suggested defaults)
KNOWN_OLLAMA_MODELS: list[str] = [
    "gemma4:e2b",
    "gemma4:e4b",
]

# Presets for quick configuration
MODEL_PRESETS: dict[str, dict[str, str]] = {
    "balanced": DEFAULT_AGENT_MODELS.copy(),
    "cost_optimized": {name: "gemini-2.5-flash-lite" for name in AGENT_NAMES},
    "quality_first": {name: "gemini-2.5-flash" for name in AGENT_NAMES},
    "local_first": {
        **{name: "gemini-2.5-flash-lite" for name in AGENT_NAMES},
        "image_describer": "ollama_chat/gemma4:e4b",
        "video_analyzer": "ollama_chat/gemma4:e4b",
        "audio_transcriber": "ollama_chat/gemma4:e4b",
    },
}


# Every LiteLLM completion provider prefix Atlas supports for agent assignments.
# Keep in sync with the proposal §"What Changes" — `agent-llm-provider-pluggable`.
SUPPORTED_PROVIDERS: tuple[str, ...] = (
    "gemini",
    "openai",
    "anthropic",
    "mistral",
    "deepseek",
    "groq",
    "together_ai",
    "xai",
    "minimax",
    "cohere",
    "ollama_chat",
    "vertex_ai",
    "bedrock",
)


def resolve_model_object(model_string: str) -> Any:
    """Convert a model string to an ADK-compatible model object.

    Behaviour depends on ``settings.llm_use_litellm_for_gemini``:

    Flag ON (default, post-cutover):
        Every provider — Gemini included — is wrapped in ``LiteLlm(...)``.
        Bare ``gemini-*`` strings are normalised to ``gemini/gemini-*``
        before wrapping so the LiteLLM router resolves them correctly.

    Flag OFF (emergency rollback):
        Gemini bare strings pass through to ADK's native ``google.genai`` path
        as before this change. Other prefixed strings still wrap in LiteLLM.

    Ollama always wraps regardless of the flag (its current behaviour).
    """
    settings = get_settings()

    if model_string.startswith("ollama_chat/"):
        os.environ.setdefault("OLLAMA_API_BASE", settings.ollama_api_base)
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(model=model_string)

    if not settings.llm_use_litellm_for_gemini:
        # Legacy native path — bare Gemini strings consumed by ADK directly.
        return model_string

    # Cutover-on: wrap every provider in LiteLLM so dispatch funnels through
    # litellm.acompletion. Normalise bare gemini-* to a fully-prefixed form first.
    if model_string.startswith("gemini-"):
        model_string = f"gemini/{model_string}"

    if "/" not in model_string:
        # No provider prefix and not a bare gemini-* — let it through unchanged.
        # ``validate_model_string`` should have caught this upstream.
        return model_string

    from google.adk.models.lite_llm import LiteLlm

    return LiteLlm(model=model_string)


def is_ollama_model(model_string: str) -> bool:
    """Check if a model string refers to an Ollama local model."""
    return model_string.startswith("ollama_chat/")


def validate_model_string(model_string: str) -> str | None:
    """Validate a model string format. Returns error message or None if valid.

    Accepts either a bare ``gemini-*`` (back-compat — implicitly the ``gemini/``
    prefix) or a fully-qualified ``<provider>/<model>`` where ``<provider>`` is
    in :data:`SUPPORTED_PROVIDERS`.
    """
    if model_string.startswith("gemini-"):
        return None
    if "/" not in model_string:
        return (
            f"Model {model_string!r} must be prefixed with a provider "
            f"(e.g. 'openai/gpt-4o-mini'). Bare 'gemini-2.5-flash' is also accepted "
            f"for backward compatibility."
        )
    prefix = model_string.split("/", 1)[0]
    if prefix not in SUPPORTED_PROVIDERS:
        return (
            f"Unsupported provider {prefix!r}. Supported: "
            f"{', '.join(SUPPORTED_PROVIDERS)}."
        )
    return None
