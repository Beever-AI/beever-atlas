"""PR-G: hydration shim that migrates legacy data into endpoints + assignments."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from scripts.migrate_to_endpoint_catalog import migrate_to_endpoint_catalog


class _AsyncCursor:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_AsyncCursor":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _Result:
    def __init__(self, matched: int = 0, deleted: int = 0) -> None:
        self.matched_count = matched
        self.modified_count = matched
        self.deleted_count = deleted


class _FakeCollection:
    def __init__(self, seed: list[dict[str, Any]] | None = None) -> None:
        self._docs: list[dict[str, Any]] = list(seed or [])

    def find(self, query: dict[str, Any], _proj: Any = None) -> _AsyncCursor:
        return _AsyncCursor([d for d in self._docs if self._matches(d, query)])

    async def find_one(self, query: dict[str, Any], _proj: Any = None) -> Any:
        for d in self._docs:
            if self._matches(d, query):
                return d
        return None

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self._docs.append(dict(doc))

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _Result:
        for d in self._docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return _Result(matched=1)
        if upsert:
            new = dict(update.get("$set", {}))
            new.update(query)
            self._docs.append(new)
        return _Result(matched=0)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if "$or" in query:
            return any(_FakeCollection._matches(doc, q) for q in query["$or"])
        return all(doc.get(k) == v for k, v in query.items())


def _stores(
    *,
    embedding_settings: dict | None = None,
    agent_model_config: dict | None = None,
) -> Any:
    return SimpleNamespace(
        mongodb=SimpleNamespace(
            db={
                "endpoints": _FakeCollection(),
                "llm_assignments": _FakeCollection(),
                "embedding_settings": _FakeCollection(
                    [embedding_settings] if embedding_settings else []
                ),
                "agent_model_config": _FakeCollection(
                    [agent_model_config] if agent_model_config else []
                ),
            }
        )
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every provider-key env so each test starts clean."""
    for v in (
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "MISTRAL_API_KEY",
        "DEEPSEEK_API_KEY",
        "GROQ_API_KEY",
        "XAI_API_KEY",
        "TOGETHER_API_KEY",
        "MINIMAX_API_KEY",
        "COHERE_API_KEY",
        "VOYAGE_API_KEY",
        "JINA_API_KEY",
        "OLLAMA_ENABLED",
        "OLLAMA_API_BASE",
        "LLM_FAST_MODEL",
    ):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)


@pytest.mark.asyncio
async def test_idempotent_when_endpoints_already_populated() -> None:
    stores = _stores()
    # Pre-populate endpoints collection so the shim should skip.
    stores.mongodb.db["endpoints"]._docs.append({"id": "existing", "name": "X"})

    result = await migrate_to_endpoint_catalog(stores)
    assert result["skipped"] == "endpoints_already_populated"
    # No new endpoints written.
    assert len(stores.mongodb.db["endpoints"]._docs) == 1


@pytest.mark.asyncio
async def test_no_legacy_data_results_in_zero_endpoints() -> None:
    """Empty env + empty legacy collections — nothing to migrate."""
    stores = _stores()
    result = await migrate_to_endpoint_catalog(stores)
    assert result["endpoints_created"] == 0
    assert result["assignments_created"] == 0


@pytest.mark.asyncio
async def test_synthesises_endpoint_from_google_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSy-test-key")
    stores = _stores()
    result = await migrate_to_endpoint_catalog(stores)
    assert result["endpoints_created"] == 1
    doc = stores.mongodb.db["endpoints"]._docs[0]
    assert doc["preset"] == "google_ai"
    assert "migrated-from-env" in doc["tags"]
    # Plaintext never leaks into the persisted doc.
    assert "AIzaSy-test-key" not in str(doc)


@pytest.mark.asyncio
async def test_synthesises_multiple_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "AIzaSy-test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    stores = _stores()
    result = await migrate_to_endpoint_catalog(stores)
    assert result["endpoints_created"] == 3
    presets = {d["preset"] for d in stores.mongodb.db["endpoints"]._docs}
    assert presets == {"google_ai", "openai", "anthropic"}


@pytest.mark.asyncio
async def test_synthesises_ollama_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    monkeypatch.setenv("OLLAMA_API_BASE", "http://localhost:11434")

    stores = _stores()
    result = await migrate_to_endpoint_catalog(stores)
    assert result["endpoints_created"] == 1
    doc = stores.mongodb.db["endpoints"]._docs[0]
    assert doc["preset"] == "ollama"
    assert doc["auth_type"] == "none"
    assert doc["base_url"].endswith("/v1")


@pytest.mark.asyncio
async def test_migrates_embedding_settings_to_assignment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JINA_API_KEY", "jina-key")
    stores = _stores(
        embedding_settings={
            "_id": "embedding_settings",
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
            "task": "text-matching",
        }
    )
    await migrate_to_endpoint_catalog(stores)
    assignments = stores.mongodb.db["llm_assignments"]._docs
    embedding = next((a for a in assignments if a["consumer"] == "embedding"), None)
    assert embedding is not None
    assert embedding["model"] == "jina-embeddings-v4"
    assert embedding["dimensions"] == 2048
    assert embedding["task"] == "text-matching"


@pytest.mark.asyncio
async def test_migrates_agent_model_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
    stores = _stores(
        agent_model_config={
            "_id": "agent_model_config",
            "models": {
                "fact_extractor": "gemini-2.5-flash",
                "qa_agent": "gemini-2.5-flash",
                "csv_mapper": "gemini-2.5-flash-lite",
            },
        }
    )
    await migrate_to_endpoint_catalog(stores)
    assignments = {a["consumer"]: a for a in stores.mongodb.db["llm_assignments"]._docs}
    assert assignments["fact_extractor"]["model"] == "gemini-2.5-flash"
    assert assignments["qa_agent"]["model"] == "gemini-2.5-flash"
    assert assignments["csv_mapper"]["model"] == "gemini-2.5-flash-lite"


@pytest.mark.asyncio
async def test_skips_agents_when_no_matching_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent model points at a provider that has no env key — skip silently."""
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
    stores = _stores(
        agent_model_config={
            "_id": "agent_model_config",
            "models": {
                "fact_extractor": "openai/gpt-4o-mini",  # no OPENAI_API_KEY
                "qa_agent": "gemini-2.5-flash",  # google_ai available
            },
        }
    )
    await migrate_to_endpoint_catalog(stores)
    assignments = {a["consumer"]: a for a in stores.mongodb.db["llm_assignments"]._docs}
    # fact_extractor was skipped, qa_agent succeeded.
    assert "fact_extractor" not in assignments
    assert "qa_agent" in assignments


@pytest.mark.asyncio
async def test_full_legacy_install_migrates_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: realistic legacy install with Gemini + Jina + Ollama."""
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")
    monkeypatch.setenv("JINA_API_KEY", "jina-test")
    monkeypatch.setenv("OLLAMA_ENABLED", "true")
    stores = _stores(
        embedding_settings={
            "_id": "embedding_settings",
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
        },
        agent_model_config={
            "_id": "agent_model_config",
            "models": {
                "fact_extractor": "gemini-2.5-flash",
                "qa_agent": "gemini-2.5-flash",
                "image_describer": "ollama_chat/gemma3:e4b",
            },
        },
    )
    result = await migrate_to_endpoint_catalog(stores)
    # 3 endpoints: google_ai, jina_ai, ollama.
    assert result["endpoints_created"] == 3
    # 3 explicit + the remaining DEFAULT_CONSUMERS (16 agents - 3 explicit = 13)
    # all fall back to LLM_FAST_MODEL=gemini-2.5-flash → google_ai endpoint.
    assert result["assignments_created"] >= 4


@pytest.mark.asyncio
async def test_returns_skipped_when_encryption_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the encryption layer raises (e.g. master key missing), surface a
    structured ``skipped`` result rather than crashing boot.

    We can't reliably unset the master key via env here (Pydantic Settings
    reads from ``.env`` regardless of ``monkeypatch.delenv``), so we patch
    ``EndpointStore.create`` to raise ``RuntimeError`` directly — the same
    behaviour ``CredentialEncryptor`` exhibits when the key is missing.
    """
    from beever_atlas.llm.endpoints import EndpointStore

    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-test")

    async def _raise(self, **_kw):
        raise RuntimeError("CREDENTIAL_MASTER_KEY is not set")

    monkeypatch.setattr(EndpointStore, "create", _raise)

    stores = _stores()
    result = await migrate_to_endpoint_catalog(stores)
    assert result["skipped"] == "credential_encryptor_unavailable"
