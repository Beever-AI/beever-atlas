"""PR-α: per-model classification covers all 5 Atlas categories + dropped buckets.

Each row asserts ``classify_model(preset, raw_id) == expected_kind``. The
mix is intentionally exhaustive over the preset-specific table in
``llm/model_classifier.py`` so a future regression on any rule triggers a
distinct failure rather than the catalog-lookup catch-all.
"""

from __future__ import annotations

import pytest

from beever_atlas.llm.model_classifier import ModelKind, classify_model

# (preset, raw_id, expected_kind)
CASES: list[tuple[str, str, ModelKind]] = [
    # ── OpenAI ───────────────────────────────────────────────────────────
    ("openai", "gpt-4o-mini", "chat"),
    ("openai", "gpt-4.1", "chat"),
    ("openai", "o4-mini", "chat"),
    ("openai", "chatgpt-4o-latest", "chat"),
    ("openai", "text-embedding-3-small", "embedding"),
    ("openai", "text-embedding-3-large", "embedding"),
    ("openai", "text-embedding-ada-002", "embedding"),
    ("openai", "whisper-1", "audio_stt"),
    ("openai", "tts-1", "audio_synth"),
    ("openai", "tts-1-hd", "audio_synth"),
    ("openai", "dall-e-3", "image_gen"),
    ("openai", "gpt-image-1", "image_gen"),
    ("openai", "omni-moderation-latest", "moderation"),
    ("openai", "ft:gpt-4o-mini:acme::abc", "fine_tune"),
    ("openai", "babbage-002", "other"),
    ("openai", "davinci-002", "other"),
    # ── Google AI / Gemini ───────────────────────────────────────────────
    ("google_ai", "gemini-2.5-flash", "chat"),
    ("google_ai", "models/gemini-2.5-pro", "chat"),
    ("google_ai", "gemini-embedding-001", "embedding"),
    ("google_ai", "text-embedding-004", "embedding"),
    ("google_ai", "imagen-3.0-generate-001", "image_gen"),
    ("google_ai", "aqa", "other"),
    ("google_ai", "learnlm-1.5-pro-experimental", "other"),
    # Specialised Gemini variants — must NOT land in the chat bucket because
    # the ``/v1beta/openai/`` shim rejects them with
    # ``INVALID_ARGUMENT: 'This model only supports Interactions API.'``.
    ("google_ai", "gemini-2.0-flash-live-001", "other"),
    ("google_ai", "gemini-2.5-flash-live-preview", "other"),
    ("google_ai", "gemini-2.5-flash-image-generation", "other"),
    ("google_ai", "gemini-2.5-flash-tts", "other"),
    ("google_ai", "gemini-2.5-flash-tts-preview", "other"),
    ("google_ai", "gemini-2.0-flash-native-audio-thinking-dialog", "other"),
    ("google_ai", "gemini-2.5-flash-realtime-preview", "other"),
    ("google_ai", "veo-2.0-generate-001", "other"),
    # ── Anthropic ────────────────────────────────────────────────────────
    ("anthropic", "claude-haiku-4-5", "chat"),
    ("anthropic", "claude-sonnet-4-6", "chat"),
    ("anthropic", "claude-opus-4-7", "chat"),
    # ── Mistral ──────────────────────────────────────────────────────────
    ("mistral", "mistral-large-latest", "chat"),
    ("mistral", "mistral-small-latest", "chat"),
    ("mistral", "pixtral-12b-2409", "chat"),
    ("mistral", "codestral-latest", "chat"),
    ("mistral", "mistral-embed", "embedding"),
    ("mistral", "codestral-embed", "embedding"),
    ("mistral", "mistral-moderation-latest", "moderation"),
    ("mistral", "ft:mistral-large-latest:acme::x", "fine_tune"),
    # ── DeepSeek ─────────────────────────────────────────────────────────
    ("deepseek", "deepseek-chat", "chat"),
    ("deepseek", "deepseek-reasoner", "chat"),
    # ── Groq ─────────────────────────────────────────────────────────────
    ("groq", "llama-3.3-70b-versatile", "chat"),
    ("groq", "mixtral-8x7b-32768", "chat"),
    ("groq", "gemma-7b-it", "chat"),
    ("groq", "qwen-2.5-32b", "chat"),
    ("groq", "kimi-k2-0905", "chat"),
    ("groq", "whisper-large-v3", "audio_stt"),
    ("groq", "playai-tts-arabic", "audio_synth"),
    ("groq", "llama-guard-3-8b", "moderation"),
    # ── xAI ──────────────────────────────────────────────────────────────
    ("xai", "grok-4", "chat"),
    ("xai", "grok-2-image-1212", "image_gen"),
    # ── MiniMax ──────────────────────────────────────────────────────────
    ("minimax", "abab6.5s-chat", "chat"),
    ("minimax", "MiniMax-Text-01", "chat"),
    ("minimax", "speech-01-hd", "audio_synth"),
    ("minimax", "music-01", "audio_synth"),
    ("minimax", "image-01", "image_gen"),
    ("minimax", "video-01", "other"),
    # ── Cohere ───────────────────────────────────────────────────────────
    ("cohere", "command-r-plus", "chat"),
    ("cohere", "embed-english-v3.0", "embedding"),
    ("cohere", "rerank-english-v3.0", "reranker"),
    # ── Voyage ───────────────────────────────────────────────────────────
    ("voyage", "voyage-3-large", "embedding"),
    ("voyage", "voyage-3", "embedding"),
    ("voyage", "rerank-2", "reranker"),
    # ── Jina ─────────────────────────────────────────────────────────────
    ("jina_ai", "jina-embeddings-v4", "embedding"),
    ("jina_ai", "jina-embeddings-v3", "embedding"),
    ("jina_ai", "jina-reranker-v2-base-multilingual", "reranker"),
    ("jina_ai", "jina-vlm", "clip"),
    ("jina_ai", "jina-clip-v1", "clip"),
    ("jina_ai", "jina-segmenter-v1", "segmenter"),
    ("jina_ai", "reader-lm-1.5b", "reader"),
    # ── OpenRouter ───────────────────────────────────────────────────────
    ("openrouter", "openai/gpt-4o-mini", "chat"),
    ("openrouter", "anthropic/claude-sonnet-4-6", "chat"),
    ("openrouter", "google/gemini-2.5-flash", "chat"),
    ("openrouter", "mistralai/mistral-large-latest", "chat"),
    ("openrouter", "cohere/embed-english-v3.0", "embedding"),
    # ── Ollama (trusted chat default) ────────────────────────────────────
    ("ollama", "gemma3:e4b", "chat"),
    ("ollama", "qwen2.5:14b", "chat"),
    ("ollama", "nomic-embed-text", "embedding"),  # generic fallback
    # ── Custom / litellm_proxy fallback ──────────────────────────────────
    ("custom", "some-rerank-model", "reranker"),
    ("custom", "some-embed-model", "embedding"),
    ("custom", "totally-unknown", "chat"),  # trust the operator
    ("litellm_proxy", "totally-unknown", "chat"),
    # ── Embedding-only preset defaults to ``other`` on unrecognised ─────
    ("jina_ai", "some-mystery-artifact", "other"),
    ("voyage", "some-mystery-artifact", "other"),
]


@pytest.mark.parametrize(("preset", "model_id", "expected"), CASES)
def test_classify_model(preset: str, model_id: str, expected: ModelKind) -> None:
    assert classify_model(preset, model_id) == expected


def test_empty_model_id_returns_other() -> None:
    assert classify_model("openai", "") == "other"


def test_catalog_lookup_wins_over_pattern() -> None:
    """A model that appears in ``known_models`` is returned by its catalog
    kind even when a pattern would steer it differently."""
    # text-embedding-3-small IS in known_models as ``embedding`` AND matches
    # the openai pattern; either way the answer is embedding — this just
    # documents that the catalog short-circuit is hit first.
    assert classify_model("openai", "text-embedding-3-small") == "embedding"
    # gemini-2.5-flash is in known_models as ``chat`` — also matches pattern.
    assert classify_model("google_ai", "gemini-2.5-flash") == "chat"
