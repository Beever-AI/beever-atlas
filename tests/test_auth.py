"""Tests for require_user API-key auth and router wiring."""

from __future__ import annotations

from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from beever_atlas.infra import auth as auth_mod
from beever_atlas.infra.auth import require_admin, require_user
from beever_atlas.infra.config import Settings


def _patch_settings(monkeypatch, **overrides):
    base = dict(api_keys="prod-key-aaaaaaaa,prod-key-bbbbbbbb", admin_token="admin-token-xyz")
    base.update(overrides)

    def fake_get_settings() -> Settings:
        return Settings(**base)

    monkeypatch.setattr(auth_mod, "get_settings", fake_get_settings)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/secret", dependencies=[Depends(require_user)])
    def secret():
        return {"ok": True}

    @app.get("/api/admin", dependencies=[Depends(require_admin)])
    def admin():
        return {"ok": True}

    return app


def test_health_public(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    assert client.get("/api/health").status_code == 200


def test_protected_missing_header(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    assert client.get("/api/secret").status_code == 401


def test_protected_wrong_key(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/api/secret", headers={"Authorization": "Bearer not-a-real-key"})
    assert r.status_code == 401


def test_protected_wrong_scheme(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/api/secret", headers={"Authorization": "Basic prod-key-aaaaaaaa"})
    assert r.status_code == 401


def test_protected_right_key(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/api/secret", headers={"Authorization": "Bearer prod-key-aaaaaaaa"})
    assert r.status_code == 200
    r2 = client.get("/api/secret", headers={"Authorization": "Bearer prod-key-bbbbbbbb"})
    assert r2.status_code == 200


def test_admin_missing_token(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    assert client.get("/api/admin").status_code == 401


def test_admin_wrong_token(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/api/admin", headers={"X-Admin-Token": "wrong"})
    assert r.status_code == 401


def test_admin_right_token(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/api/admin", headers={"X-Admin-Token": "admin-token-xyz"})
    assert r.status_code == 200


def test_no_keys_configured_rejects(monkeypatch):
    _patch_settings(monkeypatch, api_keys="")
    client = TestClient(_build_app())
    r = client.get("/api/secret", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 401
