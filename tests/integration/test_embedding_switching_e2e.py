"""End-to-end integration tests for the embedding-provider switching feature.

These tests exercise the full operator workflow from the user's perspective —
the same scenarios we promised in the migration runbook and the UI walkthrough.
External HTTP calls (to actual provider endpoints) are mocked at
``litellm.aembedding`` boundary so the tests are fast and deterministic in
CI; everything else (Settings, MongoDB writes, dim guard, runtime cache,
migration gate, re-embed script logic) runs for real.

Each scenario is a separate test so failures point at exactly one expectation
in the operator journey.

Scenarios covered:
  1. Fresh install — default Jina config boots, first embed works.
  2. Switch same-dim — PUT changes provider, next embed uses new config
     within 5s (cache TTL) or instantly (cache bust).
  3. Switch different-dim on populated install — PUT returns 409.
  4. Confirmed migration save — PUT with ``confirm_migration=true`` succeeds.
  5. Mid-migration query path — `/api/search` returns 503, agent tools
     degrade to BM25.
  6. Mid-migration sync trigger — `/api/channels/{id}/sync` returns 409.
  7. Migration job bypasses its own gate via the contextvar.
  8. Test Connection probe — happy path + 401 failure surface to caller.
  9. API key encryption round-trip — plaintext never leaks into MongoDB doc.
  10. Legacy JINA_* env vars bridge correctly into EMBEDDING_*.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api import embedding_settings as ep
from beever_atlas.infra.config import Settings
from beever_atlas.llm import embedding_health as health_mod
from beever_atlas.llm import embedding_runtime as rt
from beever_atlas.llm import embeddings as emb


# ─── Mock store ─────────────────────────────────────────────────────────


class FakeStores:
    """Single-state mock that simulates the full multi-store backend.

    Holds enough state to drive the runtime cache + migration gate
    realistically: the embedding_settings doc, the encrypted secret, the
    embedding_meta record, and a Weaviate fact count knob.
    """

    def __init__(
        self,
        *,
        settings_doc: dict | None = None,
        secret: dict | None = None,
        meta: dict | None = None,
        fact_count: int = 0,
    ):
        self.settings_doc = settings_doc
        self.secret = secret
        self.meta = meta
        self.fact_count = fact_count

        # Build the AsyncMock-shaped object the API + runtime expect.
        embedding_settings_collection = AsyncMock()

        async def _settings_find_one(_query):
            return dict(self.settings_doc) if self.settings_doc else None

        async def _settings_update_one(_query, update, **_kwargs):
            patch = update.get("$set", {})
            self.settings_doc = {**(self.settings_doc or {}), **patch}

        embedding_settings_collection.find_one = AsyncMock(side_effect=_settings_find_one)
        embedding_settings_collection.update_one = AsyncMock(side_effect=_settings_update_one)

        reembed_state_collection = AsyncMock()
        reembed_state_collection.find_one = AsyncMock(return_value=None)
        reembed_state_collection.update_one = AsyncMock()

        self._db = {
            "embedding_settings": embedding_settings_collection,
            "reembed_state": reembed_state_collection,
        }

        async def _get_secret():
            return dict(self.secret) if self.secret else None

        async def _set_secret(*, ciphertext_b64, iv_b64, tag_b64):
            self.secret = {
                "ciphertext_b64": ciphertext_b64,
                "iv_b64": iv_b64,
                "tag_b64": tag_b64,
            }

        async def _clear_secret():
            self.secret = None

        async def _get_meta():
            return dict(self.meta) if self.meta else None

        async def _set_meta(*, provider, model, dimensions, ok, error=None):
            self.meta = {
                "provider": provider,
                "model": model,
                "dimensions": dimensions,
                "last_probe_ok": ok,
                "last_probe_error": error,
            }

        self.mongodb = SimpleNamespace(
            db=self._db,
            get_embedding_secret=AsyncMock(side_effect=_get_secret),
            set_embedding_secret=AsyncMock(side_effect=_set_secret),
            clear_embedding_secret=AsyncMock(side_effect=_clear_secret),
            get_embedding_meta=AsyncMock(side_effect=_get_meta),
            set_embedding_meta=AsyncMock(side_effect=_set_meta),
        )

        async def _count_facts():
            return self.fact_count

        self.weaviate = SimpleNamespace(count_facts=AsyncMock(side_effect=_count_facts))


# ─── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def stores(monkeypatch):
    """Fresh fake stores for each scenario, plumbed everywhere the code
    looks for ``get_stores()``."""
    s = FakeStores()
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: s)
    monkeypatch.setattr("beever_atlas.api.embedding_settings.get_stores", lambda: s)
    monkeypatch.setattr("beever_atlas.api.sync.get_stores", lambda: s, raising=False)
    rt.bust_embedding_settings_cache()
    yield s
    rt.bust_embedding_settings_cache()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip every embedding-related env var so each scenario starts clean."""
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
    # Inject a deterministic CREDENTIAL_MASTER_KEY for encrypted-key tests.
    # CI runners don't have one set; without this, ``encrypt_credentials``
    # raises and the API-key encryption scenario returns 503.
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)
    # Pydantic Settings ALSO reads ``.env`` from cwd; ``delenv`` only
    # affects ``os.environ``. A dev machine whose ``.env`` contains
    # ``EMBEDDING_PROVIDER=gemini`` leaks through Settings even after
    # ``delenv`` because Pydantic reconstructs from the file.
    # ``chdir`` into a tempdir so Settings can't find ``.env`` from cwd.
    import os
    import tempfile

    _tmp = tempfile.TemporaryDirectory()
    _prev_cwd = os.getcwd()
    os.chdir(_tmp.name)
    from beever_atlas.infra.config import get_settings as _gs

    _gs.cache_clear()
    Settings._DEPRECATED_LEGACY_WARNED.clear()
    try:
        yield
    finally:
        os.chdir(_prev_cwd)
        _tmp.cleanup()


@pytest.fixture
def fake_provider(monkeypatch):
    """Replace ``litellm.aembedding`` round-trip with a deterministic fake.

    Returns the call log as a list so tests can assert what was invoked.
    Vector dim defaults to 2048 (Jina v4) but can be overridden via
    ``set_dim(n)``.
    """
    calls: list[dict[str, Any]] = []
    state = {"dim": 2048, "ok": True, "error": None}

    async def fake_call(*, model, chunk, extra_kwargs, **_kw):
        # ``_kw`` swallows the ``provider`` kwarg (and any future
        # additions) that the production ``_aembedding_call`` passes
        # through to ``dispatch_embedding``. Without this, the fake's
        # rigid signature breaks the moment the shim grows another
        # named arg, leading to opaque ``unexpected keyword argument``
        # failures in unrelated tests.
        calls.append({"model": model, "chunk": list(chunk), "extra": dict(extra_kwargs)})
        if not state["ok"]:
            raise RuntimeError(state["error"] or "fake provider failure")
        return [[0.0] * state["dim"] for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)

    def set_dim(n):
        state["dim"] = n

    def set_failure(err):
        state["ok"] = False
        state["error"] = err

    def set_success():
        state["ok"] = True
        state["error"] = None

    return SimpleNamespace(
        calls=calls, set_dim=set_dim, set_failure=set_failure, set_success=set_success
    )


@pytest.fixture
def app(stores, monkeypatch):
    """FastAPI app exposing only the endpoints under test (no auth)."""
    a = FastAPI()
    a.include_router(ep.router)

    # Sync rejection check in trigger_sync needs the gate accessible. Mount
    # a minimal version that wraps just the gate logic so we don't pull in
    # the whole sync-runner stack.
    from fastapi import APIRouter, HTTPException

    sync_router = APIRouter(prefix="/api/channels", tags=["sync"])

    @sync_router.post("/{channel_id}/sync")
    async def trigger_sync_lite(channel_id: str):  # noqa: ARG001
        if await rt.is_migration_in_progress():
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "embedding_migration_in_progress",
                    "message": "Sync paused while migration runs.",
                },
            )
        return {"status": "ok"}

    a.include_router(sync_router)

    # Reset migration registry between scenarios.
    ep._active_migration["task"] = None
    ep._active_migration["job_id"] = None
    ep._active_migration["started_at"] = None
    ep._active_migration["error"] = None

    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ─── Scenario 1: Fresh install ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_scenario_1_fresh_install_default_jina_works(stores, fake_provider):
    """Boots with no DB doc, no env override → Jina v4 defaults are used."""
    fake_provider.set_dim(2048)
    vectors = await emb.embed_texts(["hello world"])
    assert len(vectors) == 1
    assert len(vectors[0]) == 2048

    # Verify the call went out as Jina v4.
    assert len(fake_provider.calls) == 1
    assert fake_provider.calls[0]["model"] == "jina_ai/jina-embeddings-v4"
    assert fake_provider.calls[0]["extra"]["dimensions"] == 2048


# ─── Scenario 2: Switch same-dim (no migration) ────────────────────────


def test_scenario_2_put_same_dim_no_migration(stores, client):
    """PUT changes provider/model with same dim → no 409, settings persist."""
    # Voyage v3-large is 1024d; pick something at the *same* dim later.
    # We'll switch to OpenAI 3-small (1536d) but keep dim aligned to confirm
    # the same-dim path. For "no-migration" scenario we want target dim to
    # match persisted-meta dim — set persisted to 1536 first.
    stores.meta = {
        "provider": "jina_ai",
        "model": "jina-embeddings-v4",
        "dimensions": 1536,  # pretend persisted is already 1536
    }
    stores.fact_count = 100  # populated install

    resp = client.put(
        "/api/settings/embedding",
        json={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimensions": 1536,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["model"] == "text-embedding-3-small"
    assert body["dimensions"] == 1536


@pytest.mark.asyncio
async def test_scenario_2b_cache_busts_on_put(stores, fake_provider, client):
    """After PUT, the very next embed call uses the new config — no restart."""
    fake_provider.set_dim(2048)
    # First call: defaults (Jina).
    await emb.embed_texts(["before"])
    assert fake_provider.calls[-1]["model"] == "jina_ai/jina-embeddings-v4"

    # PUT a same-dim change to avoid 409.
    stores.meta = {"dimensions": 2048, "provider": "jina_ai", "model": "jina-embeddings-v4"}
    stores.fact_count = 0  # empty so no migration friction
    resp = client.put(
        "/api/settings/embedding",
        json={
            "provider": "voyage",
            "model": "voyage-3-large",
            "dimensions": 1024,
            "confirm_migration": True,
        },
    )
    assert resp.status_code == 200, resp.text

    # Cache should have been busted by the PUT handler.
    fake_provider.set_dim(1024)
    await emb.embed_texts(["after"])
    assert fake_provider.calls[-1]["model"] == "voyage/voyage-3-large"
    assert fake_provider.calls[-1]["extra"]["dimensions"] == 1024


# ─── Scenario 3: Different-dim on populated install ────────────────────


def test_scenario_3_dim_change_returns_409(stores, client):
    """PUT with dim change against populated Weaviate without
    confirm_migration → 409 with structured detail."""
    stores.meta = {"provider": "jina_ai", "model": "jina-embeddings-v4", "dimensions": 2048}
    stores.fact_count = 12_000

    resp = client.put(
        "/api/settings/embedding",
        json={
            "provider": "openai",
            "model": "text-embedding-3-large",
            "dimensions": 3072,
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "dim_mismatch_requires_migration"
    assert detail["current_dim"] == 2048
    assert detail["new_dim"] == 3072
    assert detail["fact_count"] == 12_000


# ─── Scenario 4: Confirmed migration save ───────────────────────────────


def test_scenario_4_confirm_migration_persists(stores, client):
    """Same as scenario 3 but with ``confirm_migration: true`` → 200 saved."""
    stores.meta = {"provider": "jina_ai", "model": "jina-embeddings-v4", "dimensions": 2048}
    stores.fact_count = 12_000

    resp = client.put(
        "/api/settings/embedding",
        json={
            "provider": "openai",
            "model": "text-embedding-3-large",
            "dimensions": 3072,
            "confirm_migration": True,
        },
    )
    assert resp.status_code == 200
    assert stores.settings_doc is not None
    assert stores.settings_doc["provider"] == "openai"
    assert stores.settings_doc["dimensions"] == 3072


# ─── Scenarios 5 & 6: Mid-migration query + sync ────────────────────────


@pytest.mark.asyncio
async def test_scenario_5_query_during_migration_raises(stores, fake_provider):
    """During an in-flight migration, ``embed_texts`` raises so callers
    fall back to BM25-only. (The agent-tool wrappers' existing
    ``except Exception → bm25_search`` path handles it transparently.)"""
    stores.meta = {"provider": "jina_ai", "model": "jina-embeddings-v4", "dimensions": 2048}
    stores.fact_count = 12_000
    stores.settings_doc = {
        "_id": "embedding_settings",
        "provider": "openai",
        "model": "text-embedding-3-large",
        "dimensions": 3072,
    }
    rt.bust_embedding_settings_cache()

    with pytest.raises(rt.EmbeddingMigrationInProgress):
        await emb.embed_texts(["query during migration"])


@pytest.mark.asyncio
async def test_scenario_5b_search_endpoint_returns_503_during_migration(stores):
    """`/api/search` handler returns HTTP 503 with structured error during migration."""
    from fastapi import HTTPException

    from beever_atlas.api.search import SearchRequest, search_facts
    from beever_atlas.infra.auth import Principal

    stores.meta = {"dimensions": 2048}
    stores.fact_count = 12_000
    stores.settings_doc = {
        "_id": "embedding_settings",
        "dimensions": 3072,
        "provider": "openai",
        "model": "text-embedding-3-large",
    }
    rt.bust_embedding_settings_cache()

    body = SearchRequest(query="hello", limit=5)  # channel_id=None skips access check
    principal = Principal("test-user", "user")

    with pytest.raises(HTTPException) as excinfo:
        await search_facts(body=body, principal=principal)

    assert excinfo.value.status_code == 503
    assert excinfo.value.detail["error"] == "embedding_migration_in_progress"


def test_scenario_6_sync_endpoint_returns_409_during_migration(stores, client):
    """`/api/channels/{id}/sync` returns 409 with structured body during migration."""
    stores.meta = {"dimensions": 2048}
    stores.fact_count = 12_000
    stores.settings_doc = {
        "_id": "embedding_settings",
        "dimensions": 3072,
        "provider": "openai",
        "model": "text-embedding-3-large",
    }
    rt.bust_embedding_settings_cache()

    resp = client.post("/api/channels/C123/sync")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "embedding_migration_in_progress"


# ─── Scenario 7: Migration job bypasses its own gate ────────────────────


@pytest.mark.asyncio
async def test_scenario_7_migration_contextvar_bypass(stores, fake_provider):
    """Inside the migration ContextVar, ``embed_texts`` goes through even
    when the gate would otherwise raise."""
    stores.meta = {"dimensions": 2048}
    stores.fact_count = 12_000
    stores.settings_doc = {
        "_id": "embedding_settings",
        "dimensions": 3072,
        "provider": "openai",
        "model": "text-embedding-3-large",
    }
    rt.bust_embedding_settings_cache()

    fake_provider.set_dim(3072)

    token = rt.set_migration_context(True)
    try:
        out = await emb.embed_texts(["migration job's own embed"])
        assert len(out) == 1
        assert len(out[0]) == 3072
    finally:
        rt.reset_migration_context(token)

    # Outside the contextvar, the gate fires again.
    with pytest.raises(rt.EmbeddingMigrationInProgress):
        await emb.embed_texts(["regular caller"])


# ─── Scenario 8: Test Connection probe ─────────────────────────────────


def test_scenario_8a_test_connection_happy(stores, client, monkeypatch):
    """POST /test with valid creds → ok=true, dimensions returned."""
    from beever_atlas.llm.embedding_health import EmbeddingHealth

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=1536, latency_ms=42)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)

    resp = client.post(
        "/api/settings/embedding/test",
        json={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimensions": 1536,
            "api_key": "sk-test",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["dimensions"] == 1536


def test_scenario_8b_test_connection_failure_surfaces(stores, client, monkeypatch):
    """POST /test with bad creds → ok=false, error string returned."""
    from beever_atlas.llm.embedding_health import EmbeddingHealth

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=False, dim=None, latency_ms=10, error="401 invalid api key")

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)

    resp = client.post(
        "/api/settings/embedding/test",
        json={"api_key": "sk-bad"},
    )
    assert resp.status_code == 200  # endpoint returns 200 with body.ok=false
    body = resp.json()
    assert body["ok"] is False
    assert "401" in body["error"]


# ─── Scenario 9: API key encryption round-trip ─────────────────────────


def test_scenario_9_api_key_encrypted_at_rest(stores, client):
    """PUT with api_key → ciphertext stored, plaintext absent from doc."""
    plaintext = "sk-test-DO-NOT-LEAK-1234abcd5678efgh"

    resp = client.put(
        "/api/settings/embedding",
        json={"api_key": plaintext},
    )
    assert resp.status_code == 200

    # Plaintext must not appear anywhere in the stored secret blob.
    assert stores.secret is not None
    ciphertext = base64.b64decode(stores.secret["ciphertext_b64"])
    assert plaintext.encode() not in ciphertext

    # Round-trip: decrypt with the master key recovers the plaintext.
    from beever_atlas.infra.crypto import decrypt_credentials

    iv = base64.b64decode(stores.secret["iv_b64"])
    tag = base64.b64decode(stores.secret["tag_b64"])
    decrypted = decrypt_credentials(ciphertext, iv, tag)
    assert decrypted == {"api_key": plaintext}

    # GET response masks the key — never returns plaintext.
    get_resp = client.get("/api/settings/embedding")
    assert get_resp.status_code == 200
    assert plaintext not in get_resp.text
    assert get_resp.json()["has_api_key"] is True
    assert get_resp.json()["api_key_masked"].startswith("sk-t")


# ─── Scenario 10: Legacy JINA_* env bridge ─────────────────────────────


def test_scenario_10_legacy_jina_env_bridge(monkeypatch):
    """Operator with only ``JINA_MODEL`` / ``JINA_DIMENSIONS`` set in env
    → those values populate the new ``embedding_*`` fields."""
    Settings._DEPRECATED_LEGACY_WARNED.clear()
    monkeypatch.setenv("JINA_MODEL", "jina-embeddings-v3")
    monkeypatch.setenv("JINA_DIMENSIONS", "1024")
    monkeypatch.setenv("JINA_RPM", "200")

    s = Settings()
    assert s.embedding_model == "jina-embeddings-v3"
    assert s.embedding_dimensions == 1024
    assert s.embedding_rpm == 200
    # Default provider stays "jina_ai" — no legacy alias for it.
    assert s.embedding_provider == "jina_ai"
