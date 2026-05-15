"""Heuristic capability inference for models not in ``KNOWN_MODELS``.

When an operator's custom Endpoint exposes a model name we don't recognise
(common with internal LiteLLM proxies, future model releases, fine-tunes),
the UI seeds capability checkboxes from this heuristic. Operator-set flags
on the Endpoint document remain authoritative; inference is a UX accelerator
to avoid making the operator tick every box manually.

See ``openspec/changes/agent-llm-provider-pluggable/design.md`` D5.
"""

from __future__ import annotations

from typing import Any


def infer_capabilities(model_string: str) -> dict[str, Any]:
    """Return conservative-but-useful capability flag defaults for ``model_string``.

    The heuristic is intentionally simple — substring matches against the
    lowercased model identifier. False negatives (e.g. a tool-capable model
    whose name lacks the trigger keywords) are safer than false positives —
    the operator can flip the checkbox.

    Returns a dict carrying every flag from :class:`ModelSpec` that we can
    infer; absent keys signal "no strong signal, use the conservative
    default" at the call site.
    """
    s = model_string.lower()

    # Tool-calling — covers OpenAI/Anthropic/Gemini families + Mistral + the
    # major Llama / Qwen variants that ship function-calling.
    #
    # PR-λ.4: pattern coverage for Ollama-style names. Ollama publishes
    # ``llama3.1``, ``llama3.2``, ``llama3.3`` (no hyphen) while the
    # provider APIs use ``llama-3.1`` (hyphen). Both forms denote the
    # same tool-capable model family.
    supports_tools = (
        "gpt-" in s
        or "claude" in s
        or "gemini" in s
        or "mistral" in s
        or "qwen" in s
        or "llama-3" in s
        or "llama3.1" in s
        or "llama3.2" in s
        or "llama3.3" in s
        or "minimax" in s
        or "deepseek-chat" in s
        or "grok" in s
        or "firefunction" in s
        or "nous-hermes2" in s
        # Zhipu AI / Z.AI GLM family. ``glm-4`` and ``glm-4.5`` ship native
        # function-calling (Zhipu's "tools" param mirrors OpenAI's shape).
        or "glm-4" in s
        or "chatglm" in s
    )

    # Vision — multimodal models.
    supports_vision = (
        "vision" in s
        or "-vl" in s
        or "gpt-4o" in s
        or "gpt-4.1" in s
        or "claude" in s
        or "gemini" in s
        or "llava" in s
    )

    # Audio — Whisper, Gemini multimodal, etc.
    supports_audio = "audio" in s or "whisper" in s or "gemini" in s

    # Streaming — virtually universal across modern chat APIs.
    supports_streaming = True

    # Batch — rare; conservative default.
    supports_batch = "gemini" in s  # Vertex/Gemini batch API exists

    # Embedding-specific flags.
    accepts_task = "jina" in s or "voyage" in s or "cohere/embed" in s
    accepts_dimensions = "openai/text-embedding" in s

    # Local model detection.
    local = s.startswith("ollama") or "lmstudio" in s or "vllm" in s or "localhost" in s

    return {
        "supports_tools": supports_tools,
        "supports_vision": supports_vision,
        "supports_audio": supports_audio,
        "supports_streaming": supports_streaming,
        "supports_batch": supports_batch,
        "accepts_task": accepts_task,
        "accepts_dimensions": accepts_dimensions,
        "local": local,
    }


def resolve_model_spec(
    endpoint_overrides: dict[str, dict[str, Any]] | None,
    model_id: str,
) -> dict[str, Any]:
    """Three-tier resolution per design D5:

    1. Endpoint-level operator override (``endpoint.model_overrides[model_id]``)
    2. ``KNOWN_MODELS`` catalog entry
    3. ``infer_capabilities(model)`` heuristic

    The first non-empty source wins. Result is a merged dict — operator
    override keys override catalog keys override inferred keys.
    """
    from beever_atlas.llm.known_models import lookup_by_id

    inferred: dict[str, Any] = infer_capabilities(model_id)
    catalog: dict[str, Any] = dict(lookup_by_id(model_id) or {})
    override: dict[str, Any] = {}
    if endpoint_overrides and model_id in endpoint_overrides:
        override = dict(endpoint_overrides[model_id])

    # Layered merge: inferred → catalog → override.
    merged: dict[str, Any] = {**inferred, **catalog, **override}
    return merged


__all__ = [
    "infer_capabilities",
    "resolve_model_spec",
]
