"""PR-D: preset definitions + apply_preset."""

from __future__ import annotations

import pytest

from beever_atlas.llm.assignments import DEFAULT_CONSUMERS
from beever_atlas.llm.endpoints import Endpoint
from beever_atlas.llm.presets import (
    APPLY_PRESETS,
    ENDPOINT_PRESETS,
    PresetRequirementsNotMet,
    apply_preset,
    get_endpoint_preset,
)


def _make_endpoint(preset: str, **extra: object) -> Endpoint:
    return Endpoint(
        id=f"id-{preset}",
        name=extra.get("name", preset) if isinstance(extra.get("name"), str) else preset,
        preset=preset,
        base_url="https://x",
        auth_type="api_key",
        encrypted_key=None,
        models=[],
        rpm=500,
    )


# ── ENDPOINT_PRESETS catalog ─────────────────────────────────────────────


def test_endpoint_presets_minimum_coverage() -> None:
    """Required 18+ presets present per the proposal."""
    keys = {p["key"] for p in ENDPOINT_PRESETS}
    required = {
        "google_ai",
        "openai",
        "anthropic",
        "mistral",
        "deepseek",
        "groq",
        "xai",
        "minimax",
        "cohere",
        "voyage",
        "jina_ai",
        "ollama",
        "vllm",
        "lmstudio",
        "openrouter",
        "litellm_proxy",
        "bedrock",
        "vertex_ai",
        "custom",
    }
    missing = required - keys
    assert not missing, f"missing presets: {missing}"


def test_get_endpoint_preset_returns_full_spec() -> None:
    preset = get_endpoint_preset("anthropic")
    assert preset is not None
    assert preset["base_url"] == "https://api.anthropic.com/v1"
    assert preset["auth_type"] == "api_key"
    assert "claude-sonnet-4-6" in preset["default_models"]


def test_get_endpoint_preset_unknown_returns_none() -> None:
    assert get_endpoint_preset("not-a-preset") is None


def test_bedrock_preset_uses_aws_iam_auth() -> None:
    preset = get_endpoint_preset("bedrock")
    assert preset is not None
    assert preset["auth_type"] == "aws_iam"


def test_vertex_preset_uses_google_sa_auth() -> None:
    preset = get_endpoint_preset("vertex_ai")
    assert preset is not None
    assert preset["auth_type"] == "google_sa"


def test_embedding_only_presets_flagged() -> None:
    """Voyage / Jina / Cohere ship ``embedding_only: True``."""
    for key in ("voyage", "jina_ai", "cohere"):
        preset = get_endpoint_preset(key)
        assert preset is not None
        assert preset.get("embedding_only") is True


def test_local_presets_flagged() -> None:
    """Ollama / vLLM / LM Studio ship ``local: True``."""
    for key in ("ollama", "vllm", "lmstudio"):
        preset = get_endpoint_preset(key)
        assert preset is not None
        assert preset.get("local") is True


# ── apply_preset ─────────────────────────────────────────────────────────


def test_apply_gemini_balanced() -> None:
    endpoints = [_make_endpoint("google_ai")]
    result = apply_preset("gemini-balanced", endpoints)
    assert set(result.keys()) == set(DEFAULT_CONSUMERS)
    # Embedding uses gemini-embedding-001
    assert result["embedding"].model == "gemini-embedding-001"
    assert result["embedding"].dimensions == 3072
    # qa_agent on Gemini Flash
    assert result["qa_agent"].model == "gemini-2.5-flash"


def test_apply_openai_quality() -> None:
    endpoints = [_make_endpoint("openai")]
    result = apply_preset("openai-quality", endpoints)
    assert result["qa_agent"].model == "gpt-4o"  # quality tier picks 4o
    assert result["wiki_compiler"].model == "gpt-4.1"
    assert result["embedding"].model == "text-embedding-3-large"


def test_apply_claude_quality_gemini_fast() -> None:
    endpoints = [_make_endpoint("anthropic"), _make_endpoint("google_ai")]
    result = apply_preset("claude-quality-gemini-fast", endpoints)
    # Quality: Anthropic
    assert result["qa_agent"].model == "claude-sonnet-4-6"
    assert result["wiki_compiler"].model == "claude-sonnet-4-6"
    # Ingestion: Gemini
    assert result["fact_extractor"].model == "gemini-2.5-flash"
    # Embedding: Gemini emb
    assert result["embedding"].model == "gemini-embedding-001"


def test_apply_fully_local() -> None:
    endpoints = [_make_endpoint("ollama")]
    result = apply_preset("fully-local", endpoints)
    # All consumers point at the Ollama endpoint
    assert all(a.endpoint_id == endpoints[0].id for a in result.values())
    # Embedding uses nomic
    assert result["embedding"].model == "nomic-embed-text"
    # qa_agent uses qwen2.5 (tool-capable per known_models)
    assert result["qa_agent"].model == "qwen2.5:14b"


def test_apply_gemini_balanced_missing_endpoint_raises() -> None:
    """No Google AI endpoint → PresetRequirementsNotMet."""
    with pytest.raises(PresetRequirementsNotMet) as exc:
        apply_preset("gemini-balanced", [])
    assert "google_ai" in exc.value.required


def test_apply_claude_gemini_missing_both_raises() -> None:
    with pytest.raises(PresetRequirementsNotMet) as exc:
        apply_preset("claude-quality-gemini-fast", [])
    assert set(exc.value.required) == {"anthropic", "google_ai"}


def test_apply_claude_gemini_missing_gemini_raises() -> None:
    """Only Anthropic configured → still missing Google AI."""
    with pytest.raises(PresetRequirementsNotMet) as exc:
        apply_preset("claude-quality-gemini-fast", [_make_endpoint("anthropic")])
    assert exc.value.required == ["google_ai"]


def test_apply_custom_returns_empty() -> None:
    """``custom`` is a no-op; operator configures via UI."""
    assert apply_preset("custom", []) == {}


def test_apply_unknown_preset_raises() -> None:
    with pytest.raises(ValueError):
        apply_preset("non-existent-preset", [_make_endpoint("openai")])


def test_apply_presets_metadata_in_sync_with_dispatcher() -> None:
    """Every ``APPLY_PRESETS`` key (except ``custom``) is dispatchable."""
    for key in APPLY_PRESETS:
        if key == "custom":
            continue
        # apply_preset must accept the key — we test against an empty endpoint
        # list and accept that all four real presets raise PresetRequirementsNotMet.
        # The point of this test is that the key is RECOGNISED (no ValueError).
        try:
            apply_preset(key, [])
        except PresetRequirementsNotMet:
            pass  # expected — requirements missing
        except ValueError:
            pytest.fail(f"preset {key!r} listed in APPLY_PRESETS but unknown to apply_preset")


def test_apply_gemini_balanced_picks_ollama_for_vision_when_available() -> None:
    """Vision routes to Ollama when an Ollama endpoint is configured."""
    endpoints = [_make_endpoint("google_ai"), _make_endpoint("ollama")]
    result = apply_preset("gemini-balanced", endpoints)
    assert result["image_describer"].model == "gemma3:e4b"
    assert result["image_describer"].endpoint_id == endpoints[1].id


def test_apply_gemini_balanced_falls_back_to_gemini_when_no_ollama() -> None:
    endpoints = [_make_endpoint("google_ai")]
    result = apply_preset("gemini-balanced", endpoints)
    assert result["image_describer"].model == "gemini-2.5-flash"
    assert result["image_describer"].endpoint_id == endpoints[0].id
