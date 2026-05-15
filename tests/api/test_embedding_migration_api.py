"""PR6 (settings-restructure B-i): ``/api/settings/embedding-migration`` tests.

Covers the new non-deprecated re-embed surface:
  * GET /state with a configured ``embedding`` Assignment + Endpoint returns
    the desired/persisted/migration_required shape.
  * GET /state with a ``litellm_proxy`` endpoint → reembed_supported=False
    + a reason.
  * GET /state with no ``embedding`` Assignment → all-None / not required.
  * POST /spawn with no ``embedding`` Assignment → 422 no_embedding_assignment.
  * POST /spawn with an unsupported-provider endpoint → 422
    unsupported_embedding_provider_for_reembed.
  * POST /spawn happy path → writes the legacy ``embedding_settings`` doc
    with the Assignment's provider/model/dim, returns {job_id, status}.
  * GET /status reflects the shared registry.
"""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api import embedding_migration as ep
from beever_atlas.api import embedding_settings as embedding_settings_mod
from beever_atlas.services import embedding_migration_job as job_mod


# ─── In-memory fake collection (mirrors test_endpoints_api.py) ────────────


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
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

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

    async def delete_one(self, query: dict[str, Any]) -> _Result:
        for d in list(self._docs):
            if self._matches(d, query):
                self._docs.remove(d)
                return _Result(deleted=1)
        return _Result(deleted=0)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if "$or" in query:
            return any(_FakeCollection._matches(doc, q) for q in query["$or"])
        return all(doc.get(k) == v for k, v in query.items())


def _make_stores(*, meta: dict | None = None, fact_count: int = 0) -> Any:
    endpoints_coll = _FakeCollection()
    assignments_coll = _FakeCollection()
    reembed_state = AsyncMock()
    reembed_state.find_one = AsyncMock(return_value=None)
    reembed_state.update_one = AsyncMock()
    # ``_persist_settings_doc`` writes to the ``embedding_settings`` collection.
    embedding_settings_coll = _FakeCollection()

    db = {
        "endpoints": endpoints_coll,
        "llm_assignments": assignments_coll,
        "reembed_state": reembed_state,
        "embedding_settings": embedding_settings_coll,
    }
    mongodb = SimpleNamespace(
        db=db,
        get_embedding_meta=AsyncMock(return_value=meta),
        set_embedding_meta=AsyncMock(),
        set_embedding_secret=AsyncMock(),
        clear_embedding_secret=AsyncMock(),
    )
    weaviate = SimpleNamespace(count_facts=AsyncMock(return_value=fact_count))
    return SimpleNamespace(mongodb=mongodb, weaviate=weaviate)


def _seed_endpoint(stores: Any, *, ep_id: str, preset: str, base_url: str = "https://x") -> None:
    stores.mongodb.db["endpoints"]._docs.append(
        {
            "id": ep_id,
            "name": f"endpoint-{ep_id}",
            "preset": preset,
            "base_url": base_url,
            "auth_type": "api_key",
            "encrypted_key": {"ciphertext_b64": "x", "iv_b64": "y", "tag_b64": "z"},
            "models": [],
            "rpm": 500,
        }
    )


def _seed_assignment(stores: Any, *, endpoint_id: str, model: str, dimensions: int | None) -> None:
    stores.mongodb.db["llm_assignments"]._docs.append(
        {
            "consumer": "embedding",
            "endpoint_id": endpoint_id,
            "model": model,
            "dimensions": dimensions,
            "task": None,
        }
    )


@pytest.fixture
def app_and_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """FastAPI app with the embedding-migration router only."""
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)
    # Move cwd into a tempdir so Settings doesn't pick up the dev .env.
    _tmp = tempfile.TemporaryDirectory()
    _prev = os.getcwd()
    os.chdir(_tmp.name)
    # Settings is lru-cached; ensure the injected master key is picked up so
    # the encrypt/decrypt round-trip in POST /spawn works inside these tests.
    from beever_atlas.infra.config import get_settings as _gs

    _gs.cache_clear()

    # Reset the shared migration registry between tests.
    job_mod._active_migration["task"] = None
    job_mod._active_migration["job_id"] = None
    job_mod._active_migration["started_at"] = None
    job_mod._active_migration["error"] = None

    # Reset the runtime credentials cache between tests.
    from beever_atlas.llm.agent_credentials import clear_all_runtime_credentials

    clear_all_runtime_credentials()

    app = FastAPI()
    app.include_router(ep.router)
    try:
        yield app, TestClient(app), monkeypatch
    finally:
        os.chdir(_prev)
        _tmp.cleanup()


def _wire_stores(monkeypatch: pytest.MonkeyPatch, stores: Any) -> None:
    """Point every ``get_stores`` the migration surface touches at ``stores``."""
    monkeypatch.setattr(ep, "get_stores", lambda: stores)
    # ``migration_status_snapshot`` resolves stores from its own module.
    monkeypatch.setattr(job_mod, "get_stores", lambda: stores)
    # The legacy ``_persist_settings_doc`` / ``_persist_api_key`` helpers
    # invoked by POST /spawn read ``embedding_settings.get_stores``.
    monkeypatch.setattr(embedding_settings_mod, "get_stores", lambda: stores)


# ─── GET /state ────────────────────────────────────────────────────────────


def test_state_with_configured_assignment(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores(
        meta={"provider": "jina_ai", "model": "jina-embeddings-v3", "dimensions": 1024},
        fact_count=500,
    )
    _seed_endpoint(stores, ep_id="ep-jina", preset="jina_ai", base_url="https://api.jina.ai/v1")
    _seed_assignment(stores, endpoint_id="ep-jina", model="jina-embeddings-v4", dimensions=2048)
    _wire_stores(monkeypatch, stores)

    resp = client.get("/api/settings/embedding-migration/state")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["desired_provider"] == "jina_ai"
    assert body["desired_model"] == "jina-embeddings-v4"
    assert body["desired_dimensions"] == 2048
    assert body["persisted_provider"] == "jina_ai"
    assert body["persisted_model"] == "jina-embeddings-v3"
    assert body["persisted_dimensions"] == 1024
    assert body["fact_count"] == 500
    assert body["migration_required"] is True  # 1024 != 2048, facts > 0
    assert body["reembed_supported"] is True
    assert body["reason"] is None


def test_state_google_ai_preset_maps_to_gemini(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores(meta=None, fact_count=0)
    _seed_endpoint(stores, ep_id="ep-g", preset="google_ai")
    _seed_assignment(stores, endpoint_id="ep-g", model="gemini-embedding-001", dimensions=3072)
    _wire_stores(monkeypatch, stores)

    body = client.get("/api/settings/embedding-migration/state").json()
    assert body["desired_provider"] == "gemini"
    assert body["reembed_supported"] is True


def test_state_litellm_proxy_maps_to_openai(app_and_client):
    """A ``litellm_proxy`` endpoint resolves to the ``openai`` provider (which
    IS in the legacy embedding table) — re-embed is supported via the proxy's
    OpenAI-compat shim."""
    app, client, monkeypatch = app_and_client
    stores = _make_stores(meta=None, fact_count=0)
    _seed_endpoint(stores, ep_id="ep-proxy", preset="litellm_proxy")
    _seed_assignment(stores, endpoint_id="ep-proxy", model="some-model", dimensions=1024)
    _wire_stores(monkeypatch, stores)

    body = client.get("/api/settings/embedding-migration/state").json()
    assert body["desired_provider"] == "openai"
    assert body["reembed_supported"] is True


def test_state_unsupported_provider_branch(app_and_client):
    """An ``anthropic`` endpoint → desired_provider not in SUPPORTED_PROVIDERS
    → reembed_supported=False with a helpful reason."""
    app, client, monkeypatch = app_and_client
    stores = _make_stores(meta=None, fact_count=0)
    _seed_endpoint(stores, ep_id="ep-a", preset="anthropic")
    _seed_assignment(stores, endpoint_id="ep-a", model="claude-embed", dimensions=1024)
    _wire_stores(monkeypatch, stores)

    body = client.get("/api/settings/embedding-migration/state").json()
    assert body["desired_provider"] == "anthropic"
    assert body["reembed_supported"] is False
    assert body["reason"] is not None
    assert "anthropic" in body["reason"]


def test_state_no_assignment(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores(meta=None, fact_count=0)
    _wire_stores(monkeypatch, stores)

    body = client.get("/api/settings/embedding-migration/state").json()
    assert body["migration_required"] is False
    assert body["reembed_supported"] is False
    assert body["desired_provider"] is None
    assert body["reason"] == "no embedding assignment configured"


# ─── POST /spawn ───────────────────────────────────────────────────────────


def test_spawn_no_assignment_422(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores()
    _wire_stores(monkeypatch, stores)

    resp = client.post("/api/settings/embedding-migration/spawn")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "no_embedding_assignment"


def test_spawn_endpoint_not_found_422(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores()
    _seed_assignment(stores, endpoint_id="ep-gone", model="jina-embeddings-v4", dimensions=2048)
    _wire_stores(monkeypatch, stores)

    resp = client.post("/api/settings/embedding-migration/spawn")
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "endpoint_not_found"


def test_spawn_unsupported_provider_422(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores()
    _seed_endpoint(stores, ep_id="ep-a", preset="anthropic")
    _seed_assignment(stores, endpoint_id="ep-a", model="claude-embed", dimensions=1024)
    _wire_stores(monkeypatch, stores)

    resp = client.post("/api/settings/embedding-migration/spawn")
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "unsupported_embedding_provider_for_reembed"
    assert detail["provider"] == "anthropic"
    assert detail["endpoint_preset"] == "anthropic"


def test_spawn_happy_path_writes_legacy_doc(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores()
    _seed_endpoint(stores, ep_id="ep-openai", preset="openai", base_url="https://api.openai.com/v1")
    _seed_assignment(
        stores, endpoint_id="ep-openai", model="text-embedding-3-large", dimensions=3072
    )
    _wire_stores(monkeypatch, stores)
    # Stub the spawn so no real re-embed job runs.
    monkeypatch.setattr(ep, "spawn_reembed_job", lambda: ("JOB-XYZ", "running"))

    resp = client.post("/api/settings/embedding-migration/spawn")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == "JOB-XYZ"
    assert body["status"] == "running"

    # The legacy ``embedding_settings`` doc now carries the Assignment's
    # provider/model/dim/api_base.
    persisted = stores.mongodb.db["embedding_settings"]._docs
    assert len(persisted) == 1
    doc = persisted[0]
    assert doc["provider"] == "openai"
    assert doc["model"] == "text-embedding-3-large"
    assert doc["dimensions"] == 3072
    assert doc["api_base"] == "https://api.openai.com/v1"


def test_spawn_persists_runtime_credential_when_str(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores()
    _seed_endpoint(stores, ep_id="ep-openai", preset="openai", base_url="https://api.openai.com/v1")
    _seed_assignment(
        stores, endpoint_id="ep-openai", model="text-embedding-3-large", dimensions=3072
    )
    _wire_stores(monkeypatch, stores)
    monkeypatch.setattr(ep, "spawn_reembed_job", lambda: ("JOB-1", "running"))

    # Seed a plaintext api_key for the endpoint in the runtime cache.
    from beever_atlas.llm.agent_credentials import set_runtime_credential

    set_runtime_credential("ep-openai", "sk-secret-runtime-key")

    seeded_runtime: list[str | None] = []
    from beever_atlas.llm import embeddings as embeddings_runtime

    monkeypatch.setattr(
        embeddings_runtime, "set_runtime_db_api_key", lambda v: seeded_runtime.append(v)
    )

    resp = client.post("/api/settings/embedding-migration/spawn")
    assert resp.status_code == 200, resp.text
    # The encrypted secret was persisted + the runtime key was seeded.
    stores.mongodb.set_embedding_secret.assert_awaited_once()
    assert seeded_runtime == ["sk-secret-runtime-key"]


# ─── GET /status ───────────────────────────────────────────────────────────


def test_status_idle(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores()
    _wire_stores(monkeypatch, stores)

    resp = client.get("/api/settings/embedding-migration/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is False
    assert body["job_id"] is None


def test_status_reflects_registry(app_and_client):
    app, client, monkeypatch = app_and_client
    stores = _make_stores()
    _wire_stores(monkeypatch, stores)

    job_mod._active_migration["job_id"] = "JOB-IN-REGISTRY"
    job_mod._active_migration["started_at"] = "2026-05-12T00:00:00Z"

    body = client.get("/api/settings/embedding-migration/status").json()
    assert body["job_id"] == "JOB-IN-REGISTRY"
    assert body["started_at"] == "2026-05-12T00:00:00Z"
