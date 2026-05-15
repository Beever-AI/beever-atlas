"""Unified known-models catalog covering both chat and embedding models.

Supersedes ``known_embedding_models.py`` (now a thin re-export shim). Each
entry's ``kind`` field disambiguates between chat-only, embedding-only, and
multi-purpose models. Drives:

* UI dropdowns and pill rendering on the Add Endpoint form
* Per-Assignment capability validation (block tool-needing agents from
  models without ``supports_tools``)
* Cost preview rollup
* ``accepts_task`` / ``accepts_dimensions`` kwarg forwarding to LiteLLM

Models absent from this table are still usable — the operator types the
model name manually and capability flags fall back to
``capability_infer.infer_capabilities`` (heuristic) or operator-set
checkboxes on the Endpoint document.

See ``openspec/changes/agent-llm-provider-pluggable/design.md`` D4.
"""

from __future__ import annotations

from typing import Literal, TypedDict

# All LiteLLM provider prefixes Atlas supports for completion AND embedding.
# Kept in sync with ``model_resolver.SUPPORTED_PROVIDERS``. The unified set
# is the union; embedding-only providers (jina_ai, voyage) live here.
UNIFIED_PROVIDERS: tuple[str, ...] = (
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
    "ollama",
    "vertex_ai",
    "bedrock",
    "jina_ai",
    "voyage",
)


ModelKind = Literal["chat", "embedding", "both"]


class ModelSpec(TypedDict, total=False):
    """Static metadata for a known model. All fields optional — entries set only
    what applies to the model's ``kind``."""

    kind: ModelKind
    # Chat-only fields
    context_window: int
    input_cost_per_m: float
    output_cost_per_m: float
    supports_tools: bool
    supports_vision: bool
    supports_streaming: bool
    supports_audio: bool
    supports_batch: bool
    # Embedding-only fields
    dim: int
    accepts_dimensions: bool
    accepts_task: bool
    # Common
    cost_per_m: float
    multilingual: bool
    local: bool
    notes: str


KNOWN_MODELS: dict[str, ModelSpec] = {
    # ─── Chat: Gemini 2.5 family ──────────────────────────────────────────
    "gemini/gemini-2.5-flash": {
        "kind": "chat",
        "context_window": 1_000_000,
        "input_cost_per_m": 0.30,
        "output_cost_per_m": 2.50,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "supports_audio": True,
        "supports_batch": True,
        "local": False,
    },
    "gemini/gemini-2.5-flash-lite": {
        "kind": "chat",
        "context_window": 1_000_000,
        "input_cost_per_m": 0.10,
        "output_cost_per_m": 0.40,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "supports_audio": True,
        "supports_batch": True,
        "local": False,
    },
    "gemini/gemini-2.5-pro": {
        "kind": "chat",
        "context_window": 2_000_000,
        "input_cost_per_m": 1.25,
        "output_cost_per_m": 10.00,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "supports_audio": True,
        "supports_batch": True,
        "local": False,
    },
    # ─── Chat: OpenAI 4o / 4.1 / o4 family ────────────────────────────────
    "openai/gpt-4o-mini": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 0.15,
        "output_cost_per_m": 0.60,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "local": False,
    },
    "openai/gpt-4o": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 2.50,
        "output_cost_per_m": 10.00,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "local": False,
    },
    "openai/gpt-4.1": {
        "kind": "chat",
        "context_window": 1_000_000,
        "input_cost_per_m": 3.00,
        "output_cost_per_m": 12.00,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "local": False,
    },
    "openai/o4-mini": {
        "kind": "chat",
        "context_window": 200_000,
        "input_cost_per_m": 1.10,
        "output_cost_per_m": 4.40,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    # ─── Chat: Anthropic Claude 4.x family ────────────────────────────────
    "anthropic/claude-haiku-4-5": {
        "kind": "chat",
        "context_window": 200_000,
        "input_cost_per_m": 1.00,
        "output_cost_per_m": 5.00,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "local": False,
    },
    "anthropic/claude-sonnet-4-6": {
        "kind": "chat",
        "context_window": 200_000,
        "input_cost_per_m": 3.00,
        "output_cost_per_m": 15.00,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "local": False,
    },
    "anthropic/claude-opus-4-7": {
        "kind": "chat",
        "context_window": 200_000,
        "input_cost_per_m": 15.00,
        "output_cost_per_m": 75.00,
        "supports_tools": True,
        "supports_vision": True,
        "supports_streaming": True,
        "local": False,
    },
    # ─── Chat: Mistral ────────────────────────────────────────────────────
    "mistral/mistral-small-latest": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 0.20,
        "output_cost_per_m": 0.60,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    "mistral/mistral-large-latest": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 2.00,
        "output_cost_per_m": 6.00,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    # ─── Chat: DeepSeek ───────────────────────────────────────────────────
    "deepseek/deepseek-chat": {
        "kind": "chat",
        "context_window": 64_000,
        "input_cost_per_m": 0.27,
        "output_cost_per_m": 1.10,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    "deepseek/deepseek-reasoner": {
        "kind": "chat",
        "context_window": 64_000,
        "input_cost_per_m": 0.55,
        "output_cost_per_m": 2.19,
        # Reasoner mode disables tool calling — this is the key constraint
        # gated by AGENT_CAPABILITIES for qa_agent / qa_router.
        "supports_tools": False,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    # ─── Chat: Groq ───────────────────────────────────────────────────────
    "groq/llama-3.3-70b-versatile": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 0.59,
        "output_cost_per_m": 0.79,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    "groq/mixtral-8x7b-32768": {
        "kind": "chat",
        "context_window": 32_768,
        "input_cost_per_m": 0.24,
        "output_cost_per_m": 0.24,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    # ─── Chat: xAI / MiniMax / Together ───────────────────────────────────
    "xai/grok-4": {
        "kind": "chat",
        "context_window": 256_000,
        "input_cost_per_m": 3.00,
        "output_cost_per_m": 15.00,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    "minimax/abab6.5s-chat": {
        "kind": "chat",
        "context_window": 245_000,
        "input_cost_per_m": 0.20,
        "output_cost_per_m": 0.20,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    "together_ai/meta-llama/Llama-3.3-70B-Instruct-Turbo": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 0.88,
        "output_cost_per_m": 0.88,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": False,
    },
    # ─── Chat: Ollama local ───────────────────────────────────────────────
    "ollama_chat/gemma3:e4b": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 0.0,
        "output_cost_per_m": 0.0,
        # gemma3 lacks structured tool-calling support — block qa_agent from it.
        "supports_tools": False,
        "supports_vision": True,
        "supports_streaming": True,
        "local": True,
    },
    "ollama_chat/qwen2.5:14b": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 0.0,
        "output_cost_per_m": 0.0,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": True,
    },
    "ollama_chat/qwen2.5-coder:14b": {
        "kind": "chat",
        "context_window": 32_000,
        "input_cost_per_m": 0.0,
        "output_cost_per_m": 0.0,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": True,
    },
    "ollama_chat/llama3.3": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 0.0,
        "output_cost_per_m": 0.0,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": True,
    },
    "ollama_chat/phi4": {
        "kind": "chat",
        "context_window": 16_000,
        "input_cost_per_m": 0.0,
        "output_cost_per_m": 0.0,
        "supports_tools": True,
        "supports_vision": False,
        "supports_streaming": True,
        "local": True,
    },
    "ollama_chat/llama3.2-vision": {
        "kind": "chat",
        "context_window": 128_000,
        "input_cost_per_m": 0.0,
        "output_cost_per_m": 0.0,
        "supports_tools": False,
        "supports_vision": True,
        "supports_streaming": True,
        "local": True,
    },
    # ─── Embedding entries — mirror known_embedding_models.py ─────────────
    "jina_ai/jina-embeddings-v4": {
        "kind": "embedding",
        "dim": 2048,
        "cost_per_m": 0.18,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
        "accepts_dimensions": False,
    },
    "jina_ai/jina-embeddings-v3": {
        "kind": "embedding",
        "dim": 1024,
        "cost_per_m": 0.18,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
        "accepts_dimensions": False,
    },
    "openai/text-embedding-3-large": {
        "kind": "embedding",
        "dim": 3072,
        "cost_per_m": 0.13,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
        "accepts_dimensions": True,
    },
    "openai/text-embedding-3-small": {
        "kind": "embedding",
        "dim": 1536,
        "cost_per_m": 0.02,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
        "accepts_dimensions": True,
    },
    "voyage/voyage-3-large": {
        "kind": "embedding",
        "dim": 1024,
        "cost_per_m": 0.18,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
        "accepts_dimensions": False,
    },
    "cohere/embed-english-v3.0": {
        "kind": "embedding",
        "dim": 1024,
        "cost_per_m": 0.10,
        "multilingual": False,
        "local": False,
        "accepts_task": True,
        "accepts_dimensions": False,
    },
    "cohere/embed-multilingual-v3.0": {
        "kind": "embedding",
        "dim": 1024,
        "cost_per_m": 0.10,
        "multilingual": True,
        "local": False,
        "accepts_task": True,
        "accepts_dimensions": False,
    },
    "gemini/gemini-embedding-001": {
        "kind": "embedding",
        "dim": 3072,
        "cost_per_m": 0.025,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
        "accepts_dimensions": False,
    },
    "mistral/mistral-embed": {
        "kind": "embedding",
        "dim": 1024,
        "cost_per_m": 0.10,
        "multilingual": True,
        "local": False,
        "accepts_task": False,
        "accepts_dimensions": False,
    },
    "ollama/nomic-embed-text": {
        "kind": "embedding",
        "dim": 768,
        "cost_per_m": 0.0,
        "multilingual": False,
        "local": True,
        "accepts_task": False,
        "accepts_dimensions": False,
    },
    "ollama/mxbai-embed-large": {
        "kind": "embedding",
        "dim": 1024,
        "cost_per_m": 0.0,
        "multilingual": False,
        "local": True,
        "accepts_task": False,
        "accepts_dimensions": False,
    },
}


def lookup(provider: str, model: str) -> ModelSpec | None:
    """Return the spec for a ``provider/model`` key, or ``None`` if absent."""
    return KNOWN_MODELS.get(f"{provider}/{model}")


def lookup_by_id(model_id: str) -> ModelSpec | None:
    """Return the spec by a pre-joined ``provider/model`` id, or ``None``."""
    return KNOWN_MODELS.get(model_id)


def is_known(provider: str, model: str) -> bool:
    return f"{provider}/{model}" in KNOWN_MODELS


def estimate_monthly_cost(
    assignments: list[dict],
    monthly_token_volume: dict[str, dict[str, int]] | None = None,
) -> dict:
    """Compute estimated USD/month cost from current Assignments.

    ``assignments`` is a list of dicts with at least ``consumer`` and a
    resolved ``{provider}/{model}`` id (caller pre-builds the id since this
    module does not depend on ``Endpoint`` / ``Assignment`` types).

    ``monthly_token_volume`` is a per-consumer ``{input_tokens, output_tokens}``
    estimate. When ``None`` the helper returns a structure with zero totals
    and a ``"no_activity_data"`` flag so the UI can render a "—" placeholder.

    Unknown models contribute zero and are flagged via ``cost_unknown_consumers``
    so the UI can footnote "~$X plus N unknown".
    """
    per_consumer: dict[str, float] = {}
    cost_unknown: list[str] = []
    total = 0.0

    if not monthly_token_volume:
        return {
            "total": 0.0,
            "per_consumer": {},
            "cost_unknown_consumers": [],
            "no_activity_data": True,
        }

    for assignment in assignments:
        consumer = assignment["consumer"]
        model_id = assignment.get("model_id") or assignment.get("model") or ""
        spec = lookup_by_id(model_id)
        volume = monthly_token_volume.get(consumer, {})
        if spec is None:
            per_consumer[consumer] = 0.0
            cost_unknown.append(consumer)
            continue
        # Embedding consumers use ``cost_per_m`` (single rate); chat uses input + output.
        if spec.get("kind") == "embedding":
            tokens = volume.get("input_tokens", 0)
            cost = (tokens / 1_000_000) * spec.get("cost_per_m", 0.0)
        else:
            in_tokens = volume.get("input_tokens", 0)
            out_tokens = volume.get("output_tokens", 0)
            cost = (in_tokens / 1_000_000) * spec.get("input_cost_per_m", 0.0) + (
                out_tokens / 1_000_000
            ) * spec.get("output_cost_per_m", 0.0)
        per_consumer[consumer] = round(cost, 4)
        total += cost

    return {
        "total": round(total, 4),
        "per_consumer": per_consumer,
        "cost_unknown_consumers": cost_unknown,
        "no_activity_data": False,
    }


__all__ = [
    "KNOWN_MODELS",
    "ModelKind",
    "ModelSpec",
    "UNIFIED_PROVIDERS",
    "estimate_monthly_cost",
    "is_known",
    "lookup",
    "lookup_by_id",
]
