"""PR-C: heuristic capability inference for unknown models."""

from __future__ import annotations

from beever_atlas.llm.capability_infer import infer_capabilities, resolve_model_spec


def test_gpt_4o_variant_infers_tools_and_vision() -> None:
    """GPT-4o family substrings → tools + vision."""
    caps = infer_capabilities("openai/gpt-4o-2025-future-variant")
    assert caps["supports_tools"] is True
    assert caps["supports_vision"] is True
    assert caps["supports_streaming"] is True


def test_unknown_model_conservative_defaults() -> None:
    """A model name with no trigger keywords gets tools=False, vision=False."""
    caps = infer_capabilities("custom/totally-mystery-model")
    assert caps["supports_tools"] is False
    assert caps["supports_vision"] is False
    assert caps["supports_streaming"] is True  # virtually universal
    assert caps["supports_batch"] is False


def test_ollama_prefix_infers_local() -> None:
    caps = infer_capabilities("ollama_chat/qwen2.5:32b")
    assert caps["local"] is True
    assert caps["supports_tools"] is True  # qwen2.5 family ships tool calling


def test_llava_pattern_infers_vision() -> None:
    caps = infer_capabilities("ollama_chat/llava-next:future")
    assert caps["supports_vision"] is True


def test_claude_pattern_infers_tools_vision() -> None:
    caps = infer_capabilities("anthropic/claude-9-future")
    assert caps["supports_tools"] is True
    assert caps["supports_vision"] is True


def test_jina_embedding_infers_accepts_task() -> None:
    caps = infer_capabilities("jina_ai/jina-embeddings-v5")
    assert caps["accepts_task"] is True


def test_openai_embedding_3_infers_accepts_dimensions() -> None:
    caps = infer_capabilities("openai/text-embedding-3-XL")
    assert caps["accepts_dimensions"] is True


def test_audio_models_infer_audio() -> None:
    caps = infer_capabilities("openai/whisper-1")
    assert caps["supports_audio"] is True


# ── resolve_model_spec — 3-tier precedence ───────────────────────────────


def test_resolve_uses_catalog_when_available() -> None:
    spec = resolve_model_spec(None, "anthropic/claude-sonnet-4-6")
    assert spec.get("kind") == "chat"
    assert spec.get("supports_tools") is True


def test_resolve_falls_back_to_inference_when_unknown() -> None:
    spec = resolve_model_spec(None, "openai/gpt-9999-future")
    # No catalog entry → inference fills tools+vision (from gpt-* + gpt-4o-mini-like inference).
    assert spec.get("supports_streaming") is True
    # No catalog key for kind — only inferred flags present.
    assert "kind" not in spec


def test_resolve_override_wins_over_catalog() -> None:
    """Operator-set Endpoint override beats the catalog entry."""
    spec = resolve_model_spec(
        {"openai/gpt-4o-mini": {"supports_vision": False}},
        "openai/gpt-4o-mini",
    )
    assert spec.get("supports_vision") is False  # override wins
    assert spec.get("supports_tools") is True  # catalog value preserved


def test_resolve_override_wins_over_inference() -> None:
    spec = resolve_model_spec(
        {"custom/whatever": {"supports_tools": True}},
        "custom/whatever",
    )
    assert spec.get("supports_tools") is True  # override beats inference's False
