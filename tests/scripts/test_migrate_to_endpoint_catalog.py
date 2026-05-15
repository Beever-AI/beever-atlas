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
    embedding_secret: dict | None = None,
) -> Any:
    async def _get_embedding_secret() -> dict | None:
        return embedding_secret

    return SimpleNamespace(
        mongodb=SimpleNamespace(
            db={
                "endpoints": _FakeCollection(),
                "llm_assignments": _FakeCollection(),
                "embedding_settings": _FakeCollection(
                    [embedding_settings] if embedding_settings else []
                ),
                "embedding_secret": _FakeCollection([embedding_secret] if embedding_secret else []),
                "agent_model_config": _FakeCollection(
                    [agent_model_config] if agent_model_config else []
                ),
            },
            get_embedding_secret=_get_embedding_secret,
        )
    )


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip every provider-key + embedding env so each test starts clean.

    The ``EMBEDDING_*`` vars matter because ``.env`` (loaded process-wide by
    ``server.app``'s ``load_dotenv`` when any sibling test imports it) sets
    ``EMBEDDING_PROVIDER`` etc., which would otherwise look like a legacy
    embedding signal to the shim — a false positive for the no-config tests.
    """
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
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_RPM",
        "EMBEDDING_API_BASE",
        "EMBEDDING_API_KEY",
        "EMBEDDING_TASK",
    ):
        monkeypatch.delenv(v, raising=False)


@pytest.fixture(autouse=True)
def _master_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)


@pytest.mark.asyncio
async def test_idempotent_when_endpoints_already_populated() -> None:
    stores = _stores()
    # Pre-populate endpoints collection so the shim should skip Steps 1–2 and 4.
    stores.mongodb.db["endpoints"]._docs.append({"id": "existing", "name": "X"})

    result = await migrate_to_endpoint_catalog(stores)
    assert result["skipped"] == "endpoints_already_populated"
    # No new endpoints written — no legacy embedding signal so repair is a no-op.
    assert len(stores.mongodb.db["endpoints"]._docs) == 1
    assert result["embedding_endpoint_created"] is False
    assert result["embedding_assignment_repaired"] is False


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


_FAKE_SECRET_BLOB = {
    "_id": "embedding_api_key",
    "ciphertext_b64": "Y2lwaGVy",  # base64("cipher")
    "iv_b64": "aXY=",  # base64("iv")
    "tag_b64": "dGFn",  # base64("tag")
}


@pytest.mark.asyncio
async def test_db_stored_embedding_config_creates_jina_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: an install that configured embedding via the legacy
    Settings UI — DB-stored Jina key + ``embedding_settings`` doc, **no
    ``JINA_API_KEY`` env** — must end up with a ``jina_ai`` Endpoint (tagged
    ``migrated-embedding-config``, carrying the decrypted key) AND the
    ``embedding`` Assignment pointed at it (model ``jina-embeddings-v4`` /
    dim 2048), NOT at ``google_ai``.
    """
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-agents-key")  # agents use Gemini

    # Mock the lazy ``decrypt_credentials`` import inside the shim's resolver.
    import beever_atlas.infra.crypto as crypto_mod

    monkeypatch.setattr(
        crypto_mod, "decrypt_credentials", lambda *_a, **_kw: {"api_key": "jina-db-secret"}
    )

    stores = _stores(
        embedding_settings={
            "_id": "embedding_settings",
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
        },
        embedding_secret=dict(_FAKE_SECRET_BLOB),
    )

    await migrate_to_endpoint_catalog(stores)

    endpoint_docs = stores.mongodb.db["endpoints"]._docs
    jina_ep = next((d for d in endpoint_docs if d["preset"] == "jina_ai"), None)
    assert jina_ep is not None, "expected a jina_ai endpoint to be synthesised"
    assert "migrated-embedding-config" in jina_ep["tags"]
    assert jina_ep["auth_type"] == "api_key"
    # The decrypted DB key reached EndpointStore.create (it encrypts before
    # persisting, so the plaintext shouldn't be visible in the stored doc).
    assert "jina-db-secret" not in str(jina_ep)
    assert jina_ep["encrypted_key"] is not None
    assert jina_ep["models"] == ["jina-embeddings-v4"]

    assignments = {a["consumer"]: a for a in stores.mongodb.db["llm_assignments"]._docs}
    embedding = assignments["embedding"]
    assert embedding["endpoint_id"] == jina_ep["id"]
    # Definitely NOT pointed at the google_ai endpoint.
    google_ep = next((d for d in endpoint_docs if d["preset"] == "google_ai"), None)
    assert google_ep is not None
    assert embedding["endpoint_id"] != google_ep["id"]
    assert embedding["model"] == "jina-embeddings-v4"
    assert embedding["dimensions"] == 2048


@pytest.mark.asyncio
async def test_db_stored_embedding_config_rerun_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Re-running the shim against an already-correctly-migrated install is a
    no-op — no second jina endpoint, the ``embedding`` Assignment is untouched,
    and both repair flags are False."""
    monkeypatch.setenv("GOOGLE_API_KEY", "AIza-agents-key")

    import beever_atlas.infra.crypto as crypto_mod

    monkeypatch.setattr(
        crypto_mod, "decrypt_credentials", lambda *_a, **_kw: {"api_key": "jina-db-secret"}
    )

    stores = _stores(
        embedding_settings={
            "_id": "embedding_settings",
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
        },
        embedding_secret=dict(_FAKE_SECRET_BLOB),
    )
    await migrate_to_endpoint_catalog(stores)
    endpoints_after_first = list(stores.mongodb.db["endpoints"]._docs)
    embedding_after_first = next(
        a for a in stores.mongodb.db["llm_assignments"]._docs if a["consumer"] == "embedding"
    )

    result = await migrate_to_endpoint_catalog(stores)
    assert result["skipped"] == "endpoints_already_populated"
    # Repair flags both False — nothing was wrong.
    assert result["embedding_endpoint_created"] is False
    assert result["embedding_assignment_repaired"] is False
    # Catalog state is identical to after the first run.
    assert stores.mongodb.db["endpoints"]._docs == endpoints_after_first
    jina_eps = [d for d in stores.mongodb.db["endpoints"]._docs if d["preset"] == "jina_ai"]
    assert len(jina_eps) == 1
    embedding_after_second = next(
        a for a in stores.mongodb.db["llm_assignments"]._docs if a["consumer"] == "embedding"
    )
    assert embedding_after_second == embedding_after_first


@pytest.mark.asyncio
async def test_env_embedding_provider_reuses_existing_endpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``EMBEDDING_PROVIDER=openai`` (+ ``OPENAI_API_KEY`` set) is the
    legacy signal, the embedding Assignment reuses the env-derived ``openai``
    Endpoint — no dedicated ``migrated-embedding-config`` endpoint."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    monkeypatch.setenv("EMBEDDING_PROVIDER", "openai")
    monkeypatch.setenv("EMBEDDING_MODEL", "text-embedding-3-large")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "3072")

    stores = _stores()
    result = await migrate_to_endpoint_catalog(stores)

    endpoint_docs = stores.mongodb.db["endpoints"]._docs
    # Exactly one endpoint — the env-derived openai one. No "migrated-embedding-config".
    assert result["endpoints_created"] == 1
    openai_ep = next(d for d in endpoint_docs if d["preset"] == "openai")
    assert "migrated-from-env" in openai_ep["tags"]
    assert all("migrated-embedding-config" not in d.get("tags", []) for d in endpoint_docs)

    assignments = {a["consumer"]: a for a in stores.mongodb.db["llm_assignments"]._docs}
    embedding = assignments["embedding"]
    assert embedding["endpoint_id"] == openai_ep["id"]
    assert embedding["model"] == "text-embedding-3-large"
    assert embedding["dimensions"] == 3072


@pytest.mark.asyncio
async def test_already_migrated_wrong_embedding_self_heals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The user's actual broken state: endpoints already populated with ONLY a
    ``google_ai`` endpoint, the ``embedding`` Assignment wrongly pointing at it
    with a chat-model name, plus a legacy ``embedding_settings`` doc + a
    ``secrets.embedding_api_key`` blob — no ``JINA_API_KEY`` env.

    After migrate_to_endpoint_catalog on the next server boot the shim must:
    - Create a ``jina_ai`` endpoint tagged ``migrated-embedding-config``.
    - Repair the ``embedding`` Assignment to point at it with the correct
      model / dimensions.
    - Return ``{skipped: "endpoints_already_populated",
                embedding_endpoint_created: True,
                embedding_assignment_repaired: True}``.
    """
    import beever_atlas.infra.crypto as crypto_mod

    monkeypatch.setattr(
        crypto_mod, "decrypt_credentials", lambda *_a, **_kw: {"api_key": "jina-db-secret"}
    )

    stores = _stores(
        embedding_settings={
            "_id": "embedding_settings",
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
        },
        embedding_secret=dict(_FAKE_SECRET_BLOB),
    )

    # Simulate the wrongly-migrated state: a google_ai endpoint exists and the
    # embedding Assignment points at it with a chat-model name.
    google_ep_id = "google-ep-abc123"
    stores.mongodb.db["endpoints"]._docs.append(
        {
            "id": google_ep_id,
            "name": "google_ai (from GOOGLE_API_KEY)",
            "preset": "google_ai",
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "auth_type": "api_key",
            "encrypted_key": {"ciphertext_b64": "x", "iv_b64": "y", "tag_b64": "z"},
            "models": [],
            "rpm": 1000,
            "headers": {},
            "tags": ["migrated-from-env"],
            "last_test_at": None,
            "last_test_ok": None,
            "last_test_error": None,
            "created_at": "2025-01-01T00:00:00+00:00",
            "updated_at": "2025-01-01T00:00:00+00:00",
        }
    )
    stores.mongodb.db["llm_assignments"]._docs.append(
        {
            "consumer": "embedding",
            "endpoint_id": google_ep_id,
            "model": "models/gemini-2.5-flash",  # wrong: chat model
            "dimensions": None,
            "task": None,
            "temperature": None,
            "max_tokens": None,
            "response_format": None,
            "extra_headers": {},
            "fallback_endpoint_id": None,
            "updated_at": "2025-01-01T00:00:00+00:00",
        }
    )

    result = await migrate_to_endpoint_catalog(stores)

    assert result["skipped"] == "endpoints_already_populated"
    assert result["embedding_endpoint_created"] is True
    assert result["embedding_assignment_repaired"] is True

    # A jina_ai endpoint tagged migrated-embedding-config must now exist.
    endpoint_docs = stores.mongodb.db["endpoints"]._docs
    jina_ep = next((d for d in endpoint_docs if d["preset"] == "jina_ai"), None)
    assert jina_ep is not None, "jina_ai endpoint must have been created"
    assert "migrated-embedding-config" in jina_ep["tags"]
    assert jina_ep["auth_type"] == "api_key"
    assert jina_ep["encrypted_key"] is not None

    # The embedding Assignment must now point at the jina endpoint, not google.
    assignments = {a["consumer"]: a for a in stores.mongodb.db["llm_assignments"]._docs}
    embedding = assignments["embedding"]
    assert embedding["endpoint_id"] == jina_ep["id"]
    assert embedding["endpoint_id"] != google_ep_id
    assert embedding["model"] == "jina-embeddings-v4"
    assert embedding["dimensions"] == 2048

    # The google_ai endpoint is untouched (agents still need it).
    assert any(d["id"] == google_ep_id for d in endpoint_docs)


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
