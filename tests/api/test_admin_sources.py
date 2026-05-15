"""Admin endpoints for the push-source registry.

Spec: ``openspec/changes/oss-redesign-production-wiring/specs/push-source-ingestion/``

Covers §8 of the production-wiring change:
- ``GET /api/admin/sources`` lists registered sources WITHOUT exposing
  the plaintext secret.
- ``POST /api/admin/sources`` registers a new source and returns the
  generated HMAC secret ONCE (in the response body).
- ``PATCH /api/admin/sources/{source_id}/rotate`` rotates the secret;
  old signatures stop verifying immediately.
- ``DELETE /api/admin/sources/{source_id}`` removes the row.
- All four endpoints reject requests without a valid admin token.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api.admin import router as admin_router
from beever_atlas.infra import auth as auth_mod
from beever_atlas.models.persistence import ExternalSource
from beever_atlas.stores import init_stores


_ADMIN_TOKEN = "admin-token-abc"


def _patch_admin(monkeypatch, token: str = _ADMIN_TOKEN):
    fake = SimpleNamespace(admin_token=token)
    monkeypatch.setattr(auth_mod, "get_settings", lambda: fake)


@pytest.fixture
def fake_stores(monkeypatch):
    """Wire up an in-memory ExternalSource registry on the stores singleton."""
    registry: dict[str, ExternalSource] = {}
    replay_counts: dict[str, int] = {}

    async def _get(source_id: str):
        return registry.get(source_id)

    async def _list():
        return sorted(registry.values(), key=lambda s: s.source_id)

    async def _upsert(source: ExternalSource):
        # Mirror the production behavior: derive secret_fingerprint, set
        # rotated_at when the row already exists.
        from beever_atlas.services.push_hmac import hash_secret

        fingerprint = hash_secret(source.secret)
        was_present = source.source_id in registry
        new = source.model_copy(update={"secret_fingerprint": fingerprint})
        if was_present:
            new = new.model_copy(update={"rotated_at": datetime.now(tz=UTC)})
        registry[source.source_id] = new

    async def _delete(source_id: str) -> bool:
        return registry.pop(source_id, None) is not None

    async def _replay_count(source_id: str) -> int:
        return replay_counts.get(source_id, 0)

    mongodb = SimpleNamespace(
        get_external_source=AsyncMock(side_effect=_get),
        list_external_sources=AsyncMock(side_effect=_list),
        upsert_external_source=AsyncMock(side_effect=_upsert),
        delete_external_source=AsyncMock(side_effect=_delete),
        count_idempotency_replays_for_source=AsyncMock(side_effect=_replay_count),
    )
    container = SimpleNamespace(mongodb=mongodb)
    init_stores(container)  # type: ignore[arg-type]
    return container, registry, replay_counts


@pytest.fixture
def app(monkeypatch, fake_stores):  # noqa: ARG001
    _patch_admin(monkeypatch)
    app = FastAPI()
    app.include_router(admin_router)
    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def _admin_headers() -> dict[str, str]:
    return {"X-Admin-Token": _ADMIN_TOKEN}


def test_register_source_returns_secret_once(client: TestClient, fake_stores) -> None:
    """Spec: ``POST`` returns the plaintext secret ONCE in the body, never
    on subsequent reads."""
    _, registry, _ = fake_stores

    resp = client.post(
        "/api/admin/sources",
        headers=_admin_headers(),
        json={"source_id": "openclaw-prod", "allowed_channels_pattern": "thread-*"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source_id"] == "openclaw-prod"
    secret = body["secret"]
    assert isinstance(secret, str) and len(secret) >= 32
    assert body["secret_fingerprint"]
    assert "openclaw-prod" in registry

    # The list endpoint must NOT echo the plaintext.
    list_resp = client.get("/api/admin/sources", headers=_admin_headers())
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert len(rows) == 1
    assert "secret" not in rows[0]
    assert rows[0]["secret_fingerprint"] == body["secret_fingerprint"]


def test_register_existing_source_returns_409(client: TestClient) -> None:
    payload = {"source_id": "dup-source"}
    first = client.post("/api/admin/sources", headers=_admin_headers(), json=payload)
    assert first.status_code == 201
    second = client.post("/api/admin/sources", headers=_admin_headers(), json=payload)
    assert second.status_code == 409


def test_rotate_returns_new_secret_and_invalidates_old(client: TestClient, fake_stores) -> None:
    """Spec: rotation produces a new plaintext returned ONCE; old sigs
    stop verifying immediately (secret_fingerprint changes)."""
    _, registry, _ = fake_stores

    create_resp = client.post(
        "/api/admin/sources",
        headers=_admin_headers(),
        json={"source_id": "openclaw-prod"},
    )
    assert create_resp.status_code == 201
    old_fingerprint = create_resp.json()["secret_fingerprint"]
    old_secret = create_resp.json()["secret"]

    rotate_resp = client.patch("/api/admin/sources/openclaw-prod/rotate", headers=_admin_headers())
    assert rotate_resp.status_code == 200
    new_secret = rotate_resp.json()["secret"]
    new_fingerprint = rotate_resp.json()["secret_fingerprint"]
    assert new_secret != old_secret
    assert new_fingerprint != old_fingerprint
    # Persisted row reflects the new secret + rotated_at
    persisted = registry["openclaw-prod"]
    assert persisted.secret == new_secret
    assert persisted.rotated_at is not None


def test_rotate_unknown_source_returns_404(client: TestClient) -> None:
    resp = client.patch("/api/admin/sources/missing/rotate", headers=_admin_headers())
    assert resp.status_code == 404


def test_delete_source_204_then_404(client: TestClient) -> None:
    create_resp = client.post(
        "/api/admin/sources", headers=_admin_headers(), json={"source_id": "to-delete"}
    )
    assert create_resp.status_code == 201

    del_resp = client.delete("/api/admin/sources/to-delete", headers=_admin_headers())
    assert del_resp.status_code == 204

    # Second delete returns 404
    del_again = client.delete("/api/admin/sources/to-delete", headers=_admin_headers())
    assert del_again.status_code == 404


def test_list_response_omits_plaintext_secret(client: TestClient) -> None:
    client.post("/api/admin/sources", headers=_admin_headers(), json={"source_id": "s1"})
    client.post("/api/admin/sources", headers=_admin_headers(), json={"source_id": "s2"})
    list_resp = client.get("/api/admin/sources", headers=_admin_headers())
    assert list_resp.status_code == 200
    rows = list_resp.json()
    assert sorted([r["source_id"] for r in rows]) == ["s1", "s2"]
    for row in rows:
        # The pydantic model and the serialization layer both filter out
        # plaintext — the response shape is the contract.
        assert "secret" not in row
        assert row["secret_fingerprint"]


def test_non_admin_token_rejected(client: TestClient) -> None:
    resp = client.get("/api/admin/sources")
    assert resp.status_code == 401
    resp = client.get("/api/admin/sources", headers={"X-Admin-Token": "wrong"})
    assert resp.status_code == 401
    resp = client.post(
        "/api/admin/sources",
        headers={"X-Admin-Token": "wrong"},
        json={"source_id": "x"},
    )
    assert resp.status_code == 401
