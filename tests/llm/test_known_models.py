"""PR-C: unified known-models catalog covers chat + embedding in one table."""

from __future__ import annotations

import pytest

from beever_atlas.llm.known_models import (
    KNOWN_MODELS,
    UNIFIED_PROVIDERS,
    estimate_monthly_cost,
    is_known,
    lookup,
    lookup_by_id,
)


def test_table_covers_required_providers() -> None:
    """Every provider promised in design D4 has at least one entry."""
    prefixes_present = {key.split("/", 1)[0] for key in KNOWN_MODELS}
    required = {
        "gemini",
        "openai",
        "anthropic",
        "mistral",
        "deepseek",
        "groq",
        "xai",
        "minimax",
        "together_ai",
        "ollama_chat",
        "jina_ai",
        "voyage",
        "cohere",
        "ollama",
    }
    missing = required - prefixes_present
    assert not missing, f"missing provider coverage: {missing}"


def test_chat_entries_have_required_keys() -> None:
    """Every ``kind=chat`` entry carries the chat flag set."""
    chat = {k: v for k, v in KNOWN_MODELS.items() if v.get("kind") == "chat"}
    assert len(chat) >= 20
    for model_id, spec in chat.items():
        assert "context_window" in spec, f"{model_id} missing context_window"
        assert "input_cost_per_m" in spec, f"{model_id} missing input_cost_per_m"
        assert "output_cost_per_m" in spec, f"{model_id} missing output_cost_per_m"
        assert "supports_tools" in spec, f"{model_id} missing supports_tools"
        assert "supports_vision" in spec, f"{model_id} missing supports_vision"


def test_embedding_entries_have_required_keys() -> None:
    """Every ``kind=embedding`` entry carries the embedding flag set."""
    embedding = {k: v for k, v in KNOWN_MODELS.items() if v.get("kind") == "embedding"}
    assert len(embedding) >= 8
    for model_id, spec in embedding.items():
        assert "dim" in spec, f"{model_id} missing dim"
        assert "cost_per_m" in spec, f"{model_id} missing cost_per_m"
        assert "accepts_task" in spec, f"{model_id} missing accepts_task"
        assert "accepts_dimensions" in spec, f"{model_id} missing accepts_dimensions"


def test_lookup_returns_spec() -> None:
    assert lookup("anthropic", "claude-sonnet-4-6") is not None
    assert lookup("anthropic", "claude-sonnet-4-6")["supports_tools"] is True


def test_lookup_unknown_returns_none() -> None:
    assert lookup("openai", "gpt-9000-imaginary") is None


def test_lookup_by_id_equivalent_to_lookup() -> None:
    assert lookup_by_id("anthropic/claude-sonnet-4-6") == lookup("anthropic", "claude-sonnet-4-6")


def test_is_known_smoke() -> None:
    assert is_known("openai", "gpt-4o-mini") is True
    assert is_known("openai", "imagined-model") is False


def test_deepseek_reasoner_blocks_tools() -> None:
    """The reasoner model's lack of tool support is the core test that
    motivates the validation gate."""
    spec = lookup("deepseek", "deepseek-reasoner")
    assert spec is not None
    assert spec["supports_tools"] is False


def test_ollama_gemma3_blocks_tools_but_supports_vision() -> None:
    """gemma3 is intentionally vision-yes / tools-no — drives the
    'qa_agent can't use Ollama' UX path."""
    spec = lookup("ollama_chat", "gemma3:e4b")
    assert spec is not None
    assert spec["supports_vision"] is True
    assert spec["supports_tools"] is False


def test_unified_providers_includes_embedding_and_chat() -> None:
    """The unified provider list is the union — jina_ai is in here, gemini is in here."""
    assert "jina_ai" in UNIFIED_PROVIDERS
    assert "gemini" in UNIFIED_PROVIDERS
    assert "ollama" in UNIFIED_PROVIDERS  # embedding-only ollama suffix
    assert "ollama_chat" in UNIFIED_PROVIDERS  # chat ollama suffix


# ── estimate_monthly_cost ────────────────────────────────────────────────


def test_cost_estimate_empty_volume_returns_no_activity_data() -> None:
    result = estimate_monthly_cost([], None)
    assert result["no_activity_data"] is True
    assert result["total"] == 0.0


def test_cost_estimate_chat_sums_input_plus_output() -> None:
    assignments = [{"consumer": "fact_extractor", "model_id": "anthropic/claude-haiku-4-5"}]
    volume = {"fact_extractor": {"input_tokens": 1_000_000, "output_tokens": 100_000}}
    result = estimate_monthly_cost(assignments, volume)
    # input: 1M * $1.00 = $1.00; output: 0.1M * $5.00 = $0.50; total $1.50
    assert result["total"] == pytest.approx(1.5)
    assert result["per_consumer"]["fact_extractor"] == pytest.approx(1.5)
    assert result["no_activity_data"] is False


def test_cost_estimate_embedding_uses_single_rate() -> None:
    assignments = [{"consumer": "embedding", "model_id": "jina_ai/jina-embeddings-v4"}]
    volume = {"embedding": {"input_tokens": 10_000_000, "output_tokens": 0}}
    result = estimate_monthly_cost(assignments, volume)
    # 10M * $0.18 = $1.80
    assert result["total"] == pytest.approx(1.8)


def test_cost_estimate_flags_unknown_model() -> None:
    assignments = [{"consumer": "x", "model_id": "openai/imaginary-future-model"}]
    volume = {"x": {"input_tokens": 1_000, "output_tokens": 1_000}}
    result = estimate_monthly_cost(assignments, volume)
    assert "x" in result["cost_unknown_consumers"]
    assert result["per_consumer"]["x"] == 0.0
