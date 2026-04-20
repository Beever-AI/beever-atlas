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


def resolve_model_object(model_string: str) -> Any:
    """Convert a model string to an ADK-compatible model object.

    - ``gemini-*`` or other plain strings → returned as-is (Gemini API)
    - ``ollama_chat/*`` → wrapped in ``LiteLlm(model=...)``

    Returns:
        A string (for Gemini) or LiteLlm instance (for Ollama).
    """
    if model_string.startswith("ollama_chat/"):
        settings = get_settings()
        os.environ.setdefault("OLLAMA_API_BASE", settings.ollama_api_base)
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(model=model_string)
    return model_string


def is_ollama_model(model_string: str) -> bool:
    """Check if a model string refers to an Ollama local model."""
    return model_string.startswith("ollama_chat/")


def validate_model_string(model_string: str) -> str | None:
    """Validate a model string format. Returns error message or None if valid."""
    if model_string.startswith("gemini-"):
        return None
    if model_string.startswith("ollama_chat/"):
        return None
    return (
        f"Invalid model '{model_string}'. Must start with 'gemini-' "
        f"(Gemini API) or 'ollama_chat/' (Ollama local)."
    )
