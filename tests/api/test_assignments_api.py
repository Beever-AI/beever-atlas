"""PR-E: ``/api/settings/assignments`` endpoint tests."""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api import assignments as asn_api
from beever_atlas.api import endpoints as ep_api


# Reuse the FakeCollection from the endpoints API test by inlining it here
# (test isolation > test-source DRY).


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
    return SimpleNamespace(
        mongodb=SimpleNamespace(
            db={
                "endpoints": _FakeCollection(),
                "llm_assignments": _FakeCollection(),
            }
        )
    )


@pytest.fixture
def app_and_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)
    # ``get_settings()`` is ``@lru_cache``-decorated. If any earlier import
    # already called it (e.g. via the conftest collection traversal), the
    # cached Settings has ``credential_master_key=""`` from the env state
    # at import time, and the monkeypatch above never reaches it. Clearing
    # the cache here forces the next ``get_settings()`` to re-read env
    # AFTER our setenv. Without this, ``encrypt_endpoint_credential``
    # 503s with ``credential_encryptor_unavailable`` on CI where no .env
    # exists.
    try:
        from beever_atlas.infra.config import get_settings

        get_settings.cache_clear()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        pass
    _tmp = tempfile.TemporaryDirectory()
    _prev = os.getcwd()
    os.chdir(_tmp.name)

    stores = _make_stores()
    monkeypatch.setattr("beever_atlas.api.assignments.get_stores", lambda: stores)
    monkeypatch.setattr("beever_atlas.api.endpoints.get_stores", lambda: stores)

    app = FastAPI()
    app.include_router(asn_api.router)
    app.include_router(ep_api.router)
    try:
        yield app, TestClient(app), stores
    finally:
        os.chdir(_prev)
        _tmp.cleanup()


def _seed_endpoint(
    client: TestClient, *, preset: str, models: list[str], name: str | None = None
) -> dict:
    """Helper — POSTs an Endpoint and returns the response body."""
    resp = client.post(
        "/api/settings/endpoints",
        json={
            "name": name or preset,
            "preset": preset,
            "base_url": "https://x" if preset != "ollama" else "http://localhost:11434/v1",
            "auth_type": "api_key" if preset != "ollama" else "none",
            "api_key": "sk-test" if preset != "ollama" else None,
            "models": models,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# ─── GET ────────────────────────────────────────────────────────────────


def test_list_empty(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.get("/api/settings/assignments")
    assert resp.status_code == 200
    body = resp.json()
    assert body["assignments"] == []
    # Default consumer list + capabilities exposed.
    assert "qa_agent" in body["default_consumers"]
    assert body["capabilities"]["qa_agent"] == ["tools"]


def test_get_assignment_not_found(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.get("/api/settings/assignments/qa_agent")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "assignment_not_found"


# ─── PUT ────────────────────────────────────────────────────────────────


def test_put_assignment_succeeds(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    ep = _seed_endpoint(client, preset="openai", models=["gpt-4o-mini", "gpt-4o"])
    resp = client.put(
        "/api/settings/assignments/qa_agent",
        json={"endpoint_id": ep["id"], "model": "gpt-4o", "temperature": 0.1},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["consumer"] == "qa_agent"
    assert body["model"] == "gpt-4o"
    assert body["temperature"] == 0.1


def test_put_rejects_unknown_endpoint(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.put(
        "/api/settings/assignments/fact_extractor",
        json={"endpoint_id": "no-such", "model": "x"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "endpoint_not_found"


def test_put_rejects_incompatible_qa_agent_assignment(app_and_client: Any) -> None:
    """deepseek-reasoner lacks tools → 422 with suggestions."""
    _app, client, _stores = app_and_client
    ds = _seed_endpoint(client, preset="deepseek", models=["deepseek-reasoner", "deepseek-chat"])
    # Also seed an OpenAI endpoint so suggestions can find a compatible alternative.
    _seed_endpoint(client, preset="openai", models=["gpt-4o-mini"])

    resp = client.put(
        "/api/settings/assignments/qa_agent",
        json={"endpoint_id": ds["id"], "model": "deepseek-reasoner"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "incompatible_assignment"
    assert "tools" in detail["missing_capabilities"]
    # Suggestions include at least one tool-capable model.
    assert len(detail["suggested"]) >= 1


def test_put_with_force_overrides_incompatible(app_and_client: Any) -> None:
    """``force: true`` accepts the incompatible assignment."""
    _app, client, _stores = app_and_client
    ds = _seed_endpoint(client, preset="deepseek", models=["deepseek-reasoner"])
    resp = client.put(
        "/api/settings/assignments/qa_agent",
        json={
            "endpoint_id": ds["id"],
            "model": "deepseek-reasoner",
            "force": True,
        },
    )
    assert resp.status_code == 200


def test_put_rejects_same_primary_and_fallback(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    ep = _seed_endpoint(client, preset="openai", models=["gpt-4o"])
    resp = client.put(
        "/api/settings/assignments/qa_agent",
        json={
            "endpoint_id": ep["id"],
            "model": "gpt-4o",
            "fallback_endpoint_id": ep["id"],
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "fallback_must_differ_from_primary"


def test_put_rejects_unknown_fallback(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    ep = _seed_endpoint(client, preset="openai", models=["gpt-4o"])
    resp = client.put(
        "/api/settings/assignments/qa_agent",
        json={
            "endpoint_id": ep["id"],
            "model": "gpt-4o",
            "fallback_endpoint_id": "no-such",
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "fallback_endpoint_not_found"


# ─── DELETE ─────────────────────────────────────────────────────────────


def test_delete_assignment(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    ep = _seed_endpoint(client, preset="openai", models=["gpt-4o"])
    client.put(
        "/api/settings/assignments/qa_agent",
        json={"endpoint_id": ep["id"], "model": "gpt-4o"},
    )
    resp = client.delete("/api/settings/assignments/qa_agent")
    assert resp.status_code == 204


# ─── PRESET ─────────────────────────────────────────────────────────────


def test_preset_preview_returns_diff(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    _seed_endpoint(
        client,
        preset="google_ai",
        models=["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-embedding-001"],
    )
    resp = client.post(
        "/api/settings/assignments/preset",
        json={"preset": "gemini-balanced", "confirm": False},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["action"] == "preview"
    assert len(body["diff"]) == 17  # 17 default consumers


def test_preset_apply_writes_atomically(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    _seed_endpoint(
        client,
        preset="google_ai",
        models=["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-embedding-001"],
    )
    resp = client.post(
        "/api/settings/assignments/preset",
        json={"preset": "gemini-balanced", "confirm": True},
    )
    assert resp.status_code == 200
    assert resp.json()["action"] == "applied"
    # Verify Assignments were written.
    list_resp = client.get("/api/settings/assignments")
    assert len(list_resp.json()["assignments"]) == 17


def test_preset_requirements_not_met(app_and_client: Any) -> None:
    """``claude-quality-gemini-fast`` needs both Anthropic and Google AI."""
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/assignments/preset",
        json={"preset": "claude-quality-gemini-fast", "confirm": True},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "preset_requirements_not_met"
    assert set(detail["required"]) == {"anthropic", "google_ai"}


def test_preset_unknown(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/assignments/preset",
        json={"preset": "non-existent", "confirm": False},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "unknown_preset"


def test_preset_preserves_assignments_with_custom_params(app_and_client: Any) -> None:
    """Preset apply skips Assignments with operator-set per-call params unless force."""
    _app, client, _stores = app_and_client
    google = _seed_endpoint(
        client,
        preset="google_ai",
        models=["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-embedding-001"],
    )
    # Operator hand-tunes qa_agent with a temperature.
    client.put(
        "/api/settings/assignments/qa_agent",
        json={
            "endpoint_id": google["id"],
            "model": "gemini-2.5-flash",
            "temperature": 0.0,
        },
    )
    # Apply the gemini-balanced preset — qa_agent's custom temperature should be preserved.
    resp = client.post(
        "/api/settings/assignments/preset",
        json={"preset": "gemini-balanced", "confirm": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "qa_agent" in body["preserved"]
    # qa_agent in storage retains the custom temperature.
    qa = client.get("/api/settings/assignments/qa_agent").json()
    assert qa["temperature"] == 0.0


def test_preset_force_overwrite_custom(app_and_client: Any) -> None:
    """``force_overwrite_custom: true`` resets tuned Assignments."""
    _app, client, _stores = app_and_client
    google = _seed_endpoint(
        client,
        preset="google_ai",
        models=["gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-embedding-001"],
    )
    client.put(
        "/api/settings/assignments/qa_agent",
        json={
            "endpoint_id": google["id"],
            "model": "gemini-2.5-flash",
            "temperature": 0.0,
        },
    )
    resp = client.post(
        "/api/settings/assignments/preset",
        json={
            "preset": "gemini-balanced",
            "confirm": True,
            "force_overwrite_custom": True,
        },
    )
    assert resp.status_code == 200
    qa = client.get("/api/settings/assignments/qa_agent").json()
    assert qa["temperature"] is None  # reset
