"""PR-E: ``/api/settings/endpoints`` endpoint tests."""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api import endpoints as ep_api


# ─── In-memory fake collection ────────────────────────────────────────────


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


def _make_stores() -> Any:
    endpoints_coll = _FakeCollection()
    assignments_coll = _FakeCollection()
    mongodb = SimpleNamespace(
        db={"endpoints": endpoints_coll, "llm_assignments": assignments_coll}
    )
    return SimpleNamespace(mongodb=mongodb)


@pytest.fixture
def app_and_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """FastAPI app with the endpoints router. Patches ``get_stores`` + master key."""
    # Master key for encryption.
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)

    # Move cwd into a tempdir so Settings doesn't pick up the dev .env.
    _tmp = tempfile.TemporaryDirectory()
    _prev = os.getcwd()
    os.chdir(_tmp.name)

    stores = _make_stores()
    monkeypatch.setattr("beever_atlas.api.endpoints.get_stores", lambda: stores)
    # Reset the runtime credentials cache between tests.
    from beever_atlas.llm.agent_credentials import clear_all_runtime_credentials

    clear_all_runtime_credentials()

    app = FastAPI()
    app.include_router(ep_api.router)
    try:
        yield app, TestClient(app), stores
    finally:
        os.chdir(_prev)
        _tmp.cleanup()


# ─── GET / POST ─────────────────────────────────────────────────────────


def test_list_endpoints_empty(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.get("/api/settings/endpoints")
    assert resp.status_code == 200
    assert resp.json() == {"endpoints": []}


def test_create_endpoint_encrypts_credential(app_and_client: Any) -> None:
    _app, client, stores = app_and_client
    body = {
        "name": "Anthropic prod",
        "preset": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "auth_type": "api_key",
        "api_key": "sk-ant-real-secret-XYZ1234",
        "models": ["claude-sonnet-4-6"],
        "rpm": 100,
    }
    resp = client.post("/api/settings/endpoints", json=body)
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["name"] == "Anthropic prod"
    assert payload["has_credential"] is True
    assert payload["credential_masked"] == "sk-a...1234"
    # The plaintext NEVER appears in the response.
    assert "sk-ant-real-secret" not in resp.text
    # Persisted document has only the encrypted envelope.
    persisted = stores.mongodb.db["endpoints"]._docs[0]
    assert "sk-ant-real-secret" not in str(persisted)
    assert "encrypted_key" in persisted


def test_create_endpoint_rejects_unknown_preset(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={"name": "x", "preset": "totally_made_up", "auth_type": "api_key"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "unsupported_preset"


def test_create_oauth_returns_not_implemented(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={"name": "x", "preset": "openai", "auth_type": "oauth"},
    )
    assert resp.status_code == 501
    assert resp.json()["detail"]["error"] == "oauth_not_yet_supported"


def test_get_endpoint_by_id(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    create = client.post(
        "/api/settings/endpoints",
        json={
            "name": "G",
            "preset": "openai",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-test",
        },
    ).json()
    fetched = client.get(f"/api/settings/endpoints/{create['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == create["id"]


def test_get_endpoint_not_found(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.get("/api/settings/endpoints/nonexistent-id")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "endpoint_not_found"


# ─── PUT ────────────────────────────────────────────────────────────────


def test_update_replaces_credential(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "X",
            "preset": "openai",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-old-secret-key-AAAA",
        },
    ).json()
    update = client.put(
        f"/api/settings/endpoints/{created['id']}",
        json={"api_key": "sk-new-secret-key-BBBB"},
    )
    assert update.status_code == 200
    assert update.json()["credential_masked"] == "sk-n...BBBB"


def test_update_preserves_credential_when_unspecified(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "X",
            "preset": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-ant-original-AAAA",
        },
    ).json()
    update = client.put(
        f"/api/settings/endpoints/{created['id']}",
        json={"name": "X renamed", "rpm": 50},
    )
    assert update.status_code == 200
    body = update.json()
    assert body["name"] == "X renamed"
    assert body["rpm"] == 50
    assert body["credential_masked"] == "sk-a...AAAA"


# ─── DELETE ─────────────────────────────────────────────────────────────


def test_delete_endpoint(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={"name": "X", "preset": "openai", "auth_type": "api_key", "api_key": "sk"},
    ).json()
    resp = client.delete(f"/api/settings/endpoints/{created['id']}")
    assert resp.status_code == 204
    # Subsequent GET 404s.
    assert client.get(f"/api/settings/endpoints/{created['id']}").status_code == 404


def test_delete_protected_when_in_use(app_and_client: Any) -> None:
    """An Assignment referencing the Endpoint blocks DELETE with 409."""
    _app, client, stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={"name": "X", "preset": "openai", "auth_type": "api_key", "api_key": "sk-x"},
    ).json()

    # Inject an Assignment referencing this Endpoint directly into the store.
    stores.mongodb.db["llm_assignments"]._docs.append(
        {"consumer": "qa_agent", "endpoint_id": created["id"], "model": "gpt-4o"}
    )

    resp = client.delete(f"/api/settings/endpoints/{created['id']}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "endpoint_in_use_as_primary_or_fallback"
    assert "qa_agent" in detail["consumers"]


# ─── Discover ───────────────────────────────────────────────────────────


def test_discover_unknown_endpoint(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.post("/api/settings/endpoints/nope/discover")
    assert resp.status_code == 404


def test_discover_returns_models_for_ollama(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama discovery hits the native ``/api/tags`` endpoint."""
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "ollama",
            "preset": "ollama",
            "base_url": "http://localhost:11434/v1",
            "auth_type": "none",
        },
    ).json()

    async def fake_discover(endpoint, **_kw):  # noqa: ANN001
        return {"ok": True, "models": ["gemma3:e4b", "qwen2.5:14b"]}

    monkeypatch.setattr("beever_atlas.api.endpoints.discover_models", fake_discover)
    resp = client.post(f"/api/settings/endpoints/{created['id']}/discover")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "gemma3:e4b" in body["models"]


# ─── Plaintext absence ─────────────────────────────────────────────────


def test_list_response_never_includes_plaintext(app_and_client: Any) -> None:
    """An end-to-end audit: across create + list + get, no plaintext leaks."""
    _app, client, _stores = app_and_client
    secret = "VERY-SECRET-VALUE-DO-NOT-LEAK"
    client.post(
        "/api/settings/endpoints",
        json={
            "name": "leak-test",
            "preset": "openai",
            "base_url": "https://x",
            "auth_type": "api_key",
            "api_key": secret,
        },
    )
    list_text = client.get("/api/settings/endpoints").text
    assert secret not in list_text
