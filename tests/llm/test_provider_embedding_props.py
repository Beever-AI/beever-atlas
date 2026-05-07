"""PR-B: ``LLMProvider.embedding_*`` properties reflect ``EmbeddingSettings``.

The previous implementation read ``settings.jina_model`` / ``jina_dimensions``;
after the rename they MUST read the generic ``embedding_*`` fields so a
provider switch flips the model surfaced through the activity feed.
"""

from __future__ import annotations

from beever_atlas.infra.config import Settings
from beever_atlas.llm.provider import LLMProvider


def _clear_env(monkeypatch):
    for var in (
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_RPM",
        "EMBEDDING_API_BASE",
        "EMBEDDING_API_KEY",
        "EMBEDDING_TASK",
        "JINA_API_URL",
        "JINA_MODEL",
        "JINA_DIMENSIONS",
        "JINA_RPM",
    ):
        monkeypatch.delenv(var, raising=False)


def test_embedding_props_default_to_jina_v4(monkeypatch):
    _clear_env(monkeypatch)
    Settings._DEPRECATED_LEGACY_WARNED.clear()
    provider = LLMProvider(Settings())
    assert provider.embedding_provider == "jina_ai"
    assert provider.embedding_model == "jina-embeddings-v4"
    assert provider.embedding_dimensions == 2048


def test_embedding_props_reflect_new_env_override(monkeypatch):
    _clear_env(monkeypatch)
    Settings._DEPRECATED_LEGACY_WARNED.clear()
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "3072")
    provider = LLMProvider(Settings())
    assert provider.embedding_provider == "openai"
    assert provider.embedding_model == "text-embedding-3-large"
    assert provider.embedding_dimensions == 3072


def test_embedding_props_reflect_legacy_jina_alias(monkeypatch):
    """An install on only ``JINA_MODEL`` still surfaces the right model
    through ``LLMProvider.embedding_model`` thanks to the alias bridge."""
    _clear_env(monkeypatch)
    Settings._DEPRECATED_LEGACY_WARNED.clear()
    monkeypatch.setenv("JINA_MODEL", "jina-embeddings-v3")
    monkeypatch.setenv("JINA_DIMENSIONS", "1024")
    provider = LLMProvider(Settings())
    assert provider.embedding_model == "jina-embeddings-v3"
    assert provider.embedding_dimensions == 1024


def test_embedding_props_unaffected_by_chat_overrides(monkeypatch):
    """``reload`` populates per-agent chat-model overrides; embedding props
    stay sourced from Settings and are NOT mutated by chat reloads."""
    _clear_env(monkeypatch)
    Settings._DEPRECATED_LEGACY_WARNED.clear()
    provider = LLMProvider(Settings())
    provider.reload({"fact_extractor": "ollama_chat/llama3"})
    assert provider.embedding_model == "jina-embeddings-v4"
