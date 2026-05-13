"""Hand-curated table of supported embedding models.

Drives:
  * UI auto-fill for the Settings card (dimension on model pick).
  * Cost preview for the re-embed migration banner.
  * Multilingual / Local pill badges.
  * Dropping the Jina-specific ``task=`` kwarg for providers that don't accept it.

Models not in this table are still usable — the operator just types the
dimension manually and the cost preview shows "verify after Test Connection".
"""

from __future__ import annotations

from typing import Literal, TypedDict

ProviderPrefix = Literal[
    "jina_ai",
    "openai",
    "cohere",
    "voyage",
    "gemini",
    "mistral",
    "ollama",
    "bedrock",
    "vertex_ai",
]

SUPPORTED_PROVIDERS: tuple[ProviderPrefix, ...] = (
    "jina_ai",
    "openai",
    "cohere",
    "voyage",
    "gemini",
    "mistral",
    "ollama",
    "bedrock",
    "vertex_ai",
)


class EmbeddingModelSpec(TypedDict):
    """Static metadata for a known embedding model."""

    dim: int
    cost_per_m: float  # USD per million tokens; 0.0 = free / local
    multilingual: bool
    local: bool
    accepts_task: bool  # true ⇒ provider honours the Jina-style ``task=`` kwarg


KNOWN_EMBEDDING_MODELS: dict[str, EmbeddingModelSpec] = {
    # Jina v4 — current default. 2048d, multilingual, accepts ``task=text-matching``.
    "jina_ai/jina-embeddings-v4": {
        "dim": 2048,
        "cost_per_m": 0.18,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
    },
    "jina_ai/jina-embeddings-v3": {
        "dim": 1024,
        "cost_per_m": 0.18,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
    },
    # Jina v2 base (English) — legacy 768d model, still served on api.jina.ai.
    "jina_ai/jina-embeddings-v2-base-en": {
        "dim": 768,
        "cost_per_m": 0.05,
        "multilingual": False,
        "local": False,
        "accepts_task": True,
    },
    # OpenAI v3 family — multilingual, supports ``dimensions=`` resizing.
    "openai/text-embedding-3-large": {
        "dim": 3072,
        "cost_per_m": 0.13,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
    },
    "openai/text-embedding-3-small": {
        "dim": 1536,
        "cost_per_m": 0.02,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
    },
    # OpenAI ada-002 — the legacy 1536d model, still served.
    "openai/text-embedding-ada-002": {
        "dim": 1536,
        "cost_per_m": 0.10,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
    },
    # Voyage — accepts a ``task=`` kwarg through LiteLLM (input_type translation).
    "voyage/voyage-3-large": {
        "dim": 1024,
        "cost_per_m": 0.18,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
    },
    "voyage/voyage-3": {
        "dim": 1024,
        "cost_per_m": 0.06,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
    },
    "voyage/voyage-3-lite": {
        "dim": 512,
        "cost_per_m": 0.02,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
    },
    # Cohere v3 — English-only, multilingual variants below.
    "cohere/embed-english-v3.0": {
        "dim": 1024,
        "cost_per_m": 0.10,
        "multilingual": False,
        "local": False,
        "accepts_task": True,  # input_type
    },
    "cohere/embed-multilingual-v3.0": {
        "dim": 1024,
        "cost_per_m": 0.10,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
    },
    # Cohere Embed v4 — unified multilingual 1536d model.
    "cohere/embed-v4.0": {
        "dim": 1536,
        "cost_per_m": 0.12,
        "multilingual": True,
        "local": False,
        "accepts_task": True,  # input_type
    },
    # Gemini text-embedding-004 — Google's per-request embedding endpoint,
    # 768d, multilingual; currently free-tier on the AI Studio key.
    "gemini/text-embedding-004": {
        "dim": 768,
        "cost_per_m": 0.0,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
    },
    # Gemini gemini-embedding-001 — GA model. 3072 dims by default; supports
    # Matryoshka truncation via ``outputDimensionality`` (128/256/.../3072)
    # but we ship the natural dim so users don't get silent dim drift.
    # Currently free-tier on the AI Studio key (per-request embeddings).
    "gemini/gemini-embedding-001": {
        "dim": 3072,
        "cost_per_m": 0.0,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
    },
    # Mistral — single hosted general embedding endpoint + a code-specialised one.
    "mistral/mistral-embed": {
        "dim": 1024,
        "cost_per_m": 0.10,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
    },
    "mistral/codestral-embed": {
        "dim": 1536,
        "cost_per_m": 0.15,
        "multilingual": False,
        "local": False,
        "accepts_task": False,
    },
    # Ollama — local, free; quality varies by model. nomic-embed-text is 768d English-leaning.
    "ollama/nomic-embed-text": {
        "dim": 768,
        "cost_per_m": 0.0,
        "multilingual": False,
        "local": True,
        "accepts_task": False,
    },
    "ollama/mxbai-embed-large": {
        "dim": 1024,
        "cost_per_m": 0.0,
        "multilingual": False,
        "local": True,
        "accepts_task": False,
    },
    # BGE-M3 — strong multilingual local model (BAAI).
    "ollama/bge-m3": {
        "dim": 1024,
        "cost_per_m": 0.0,
        "multilingual": True,
        "local": True,
        "accepts_task": False,
    },
    # Snowflake Arctic Embed 2 — multilingual local model.
    "ollama/snowflake-arctic-embed2": {
        "dim": 1024,
        "cost_per_m": 0.0,
        "multilingual": True,
        "local": True,
        "accepts_task": False,
    },
    # all-MiniLM — tiny 384d English local model; fast but lower recall.
    "ollama/all-minilm": {
        "dim": 384,
        "cost_per_m": 0.0,
        "multilingual": False,
        "local": True,
        "accepts_task": False,
    },
}


def lookup_model(provider: str, model: str) -> EmbeddingModelSpec | None:
    """Return the static spec for a `provider/model` pair, or None if unknown."""
    return KNOWN_EMBEDDING_MODELS.get(f"{provider}/{model}")


def model_accepts_task(provider: str, model: str) -> bool:
    """True when the provider/model is known to accept a ``task=`` kwarg.

    Unknown models default to True — LiteLLM ``drop_params=True`` will drop the
    kwarg if the provider rejects it, so a permissive default is safe and lets
    new Jina/Voyage models work before this table is updated.
    """
    spec = lookup_model(provider, model)
    return spec["accepts_task"] if spec is not None else True


def is_known(provider: str, model: str) -> bool:
    return f"{provider}/{model}" in KNOWN_EMBEDDING_MODELS


__all__ = [
    "EmbeddingModelSpec",
    "KNOWN_EMBEDDING_MODELS",
    "ProviderPrefix",
    "SUPPORTED_PROVIDERS",
    "is_known",
    "lookup_model",
    "model_accepts_task",
]
