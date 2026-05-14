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


def resolve_model_object(
    model_string: str,
    *,
    api_key: str | None = None,
    api_base: str | None = None,
) -> Any:
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

    PR-ν.1: ``api_key`` and ``api_base`` — when provided — are forwarded to
    the ``LiteLlm`` constructor so per-Endpoint credentials reach the
    underlying ``litellm.acompletion`` call. Without these, an agent using
    a custom-preset endpoint (e.g. Z.AI's GLM via ``openai`` provider) hits
    LiteLLM's fallback path of reading ``OPENAI_API_KEY`` from env and
    400s with ``AuthenticationError``. Pass-through is optional so the
    legacy callers that don't know about per-Endpoint credentials still
    work.
    """
    settings = get_settings()

    extra: dict[str, Any] = {}
    if api_key:
        extra["api_key"] = api_key
    if api_base:
        extra["api_base"] = api_base

    if model_string.startswith("ollama_chat/"):
        os.environ.setdefault("OLLAMA_API_BASE", settings.ollama_api_base)
        from google.adk.models.lite_llm import LiteLlm

        return LiteLlm(model=model_string, **extra)

    if not settings.llm_use_litellm_for_gemini:
        # The flag name is Gemini-specific. When False:
        #   * Gemini bypasses LiteLlm → returned as a BARE string for ADK's
        #     native ``Gemini`` model class.
        #   * Every OTHER provider (openai/anthropic/mistral/deepseek/groq/
        #     minimax/together_ai/xai/cohere/…) still needs LiteLlm wrapping
        #     — ADK has no native client for those.
        #
        # Strip the ``gemini/`` prefix so the agent path works even when
        # callers upstream (``LLMProvider.resolve_model`` via
        # ``route_for_endpoint``, ``reload_from_db`` re-prefixing
        # ``Assignment.model``) hand us the prefixed shape. Without this
        # strip, ADK raises:
        #   ValueError: Model gemini/gemini-2.5-flash not found.
        #   Provider-style models require the litellm package.
        # — and the entire extraction pipeline fails with ``errors=N`` on
        # every batch.
        if model_string.startswith("gemini/"):
            return model_string[len("gemini/") :]
        if model_string.startswith("gemini-"):
            return model_string  # already bare Gemini
        if "/" in model_string:
            # Any non-Gemini provider prefix → LiteLlm wrap. The flag only
            # disables LiteLlm for Gemini; OpenAI/Anthropic/Mistral/etc.
            # have no native ADK client and must go through LiteLLM.
            from google.adk.models.lite_llm import LiteLlm

            return LiteLlm(model=model_string, **extra)
        # Bare non-Gemini string (no prefix, no provider) — return as-is and
        # let ADK figure it out. ``validate_model_string`` upstream should
        # have rejected this shape already.
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

    return LiteLlm(model=model_string, **extra)


def is_ollama_model(model_string: str) -> bool:
    """Check if a model string refers to an Ollama local model."""
    return model_string.startswith("ollama_chat/")


# Per-agent capability requirements. The dispatch + UI validation gate
# rejects assignments where the model lacks every flag listed for the agent.
# See design D5 in ``openspec/changes/agent-llm-provider-pluggable/``.
AGENT_CAPABILITIES: dict[str, set[str]] = {
    "qa_agent": {"tools"},
    "qa_router": {"tools"},
    "image_describer": {"vision"},
    "video_analyzer": {"vision"},
    "document_digester": {"vision"},
    "audio_transcriber": {"audio"},
    # ``structured-output`` is recorded for future use — no current flag.
    "csv_mapper": {"structured-output"},
    "decomposer": {"structured-output"},
}


def _capability_to_spec_key(capability: str) -> str:
    """Map a capability token (``tools``) to the ``ModelSpec`` flag (``supports_tools``)."""
    return {
        "tools": "supports_tools",
        "vision": "supports_vision",
        "audio": "supports_audio",
        "streaming": "supports_streaming",
        "batch": "supports_batch",
        "structured-output": None,  # not yet a flag — always passes
    }.get(capability, "")


def validate_assignment_compatibility(
    consumer: str,
    model_id: str,
    endpoint_overrides: dict[str, dict[str, Any]] | None = None,
) -> list[str]:
    """Return the list of capability tokens the (consumer, model) pair lacks.

    Empty list = compatible. Non-empty = a 422 should be surfaced with these
    missing capabilities + suggested alternatives.

    ``model_id`` is the fully-qualified ``provider/model`` string.
    ``endpoint_overrides`` is the Endpoint's per-model flag override map
    (operator-set checkboxes on the Add Endpoint form). Resolution follows
    the three-tier precedence in :func:`capability_infer.resolve_model_spec`.
    """
    from beever_atlas.llm.capability_infer import resolve_model_spec

    required = AGENT_CAPABILITIES.get(consumer, set())
    if not required:
        return []
    spec = resolve_model_spec(endpoint_overrides, model_id)

    missing: list[str] = []
    for cap in required:
        spec_key = _capability_to_spec_key(cap)
        if not spec_key:
            # Unmapped capability tokens (structured-output) always pass for now.
            continue
        if not bool(spec.get(spec_key, False)):
            missing.append(cap)
    return missing


def suggest_compatible_assignments(
    consumer: str,
    candidate_models: list[tuple[str, str]],
    n: int = 3,
) -> list[tuple[str, str]]:
    """Return up to ``n`` ``(endpoint_id, model_id)`` pairs satisfying the
    capability set for ``consumer``, sorted by ascending input cost (local
    models preferred when cost matches).

    ``candidate_models`` is a flat list of ``(endpoint_id, model_id)`` from
    the operator's existing Endpoints — caller assembles it.
    """
    from beever_atlas.llm.capability_infer import resolve_model_spec

    required = AGENT_CAPABILITIES.get(consumer, set())
    compatible: list[tuple[float, bool, str, str]] = []  # (cost, is_local, ep_id, model)
    for ep_id, model_id in candidate_models:
        spec = resolve_model_spec(None, model_id)
        ok = True
        for cap in required:
            spec_key = _capability_to_spec_key(cap)
            if spec_key and not bool(spec.get(spec_key, False)):
                ok = False
                break
        if not ok:
            continue
        cost = float(spec.get("input_cost_per_m", 0.0))
        is_local = bool(spec.get("local", False))
        # Local models sort BEFORE cloud at the same nominal cost — boost via
        # negative-rank sentinel.
        compatible.append((cost, not is_local, ep_id, model_id))

    compatible.sort(key=lambda t: (t[1], t[0]))  # local-first, then cost
    return [(ep_id, m) for _, _, ep_id, m in compatible[:n]]


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
        return f"Unsupported provider {prefix!r}. Supported: {', '.join(SUPPORTED_PROVIDERS)}."
    return None
