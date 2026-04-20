"""Tests for the typed Principal + split require_user / require_bridge deps.

Covers the `principal-auth` capability: stable ids, key-material hygiene,
user-vs-bridge kind filtering, and the transitional
``BEEVER_ALLOW_BRIDGE_AS_USER`` flag that closes security finding H4 when
flipped to False.
"""

from __future__ import annotations

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from beever_atlas.infra import auth as auth_mod
from beever_atlas.infra.auth import Principal, require_bridge, require_user
from beever_atlas.infra.config import Settings


def _patch_settings(monkeypatch, **overrides):
    base = dict(
        api_keys="user-key-aaaaaaaa,user-key-bbbbbbbb",
        bridge_api_key="bridge-secret-xxxxxxxx",
        admin_token="admin-token-xyz",
        allow_bridge_as_user=True,
    )
    base.update(overrides)

    def fake_get_settings() -> Settings:
        return Settings(**base)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mod, "get_settings", fake_get_settings)


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/user", dependencies=[Depends(require_user)])
    def user_route():
        return {"ok": True}

    @app.get("/internal", dependencies=[Depends(require_bridge)])
    def internal_route():
        return {"ok": True}

    @app.get("/user-principal")
    def user_principal(p: Principal = Depends(require_user)):
        return {"kind": p.kind, "id": p.id}

    return app


def test_principal_is_string_compatible(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/user-principal", headers={"Authorization": "Bearer user-key-aaaaaaaa"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "user"
    assert body["id"].startswith("user:")


def test_principal_id_stable_across_requests(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r1 = client.get("/user-principal", headers={"Authorization": "Bearer user-key-aaaaaaaa"})
    r2 = client.get("/user-principal", headers={"Authorization": "Bearer user-key-aaaaaaaa"})
    assert r1.status_code == r2.status_code == 200
    assert r1.json()["id"] == r2.json()["id"]


def test_different_user_keys_produce_different_ids(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r1 = client.get("/user-principal", headers={"Authorization": "Bearer user-key-aaaaaaaa"})
    r2 = client.get("/user-principal", headers={"Authorization": "Bearer user-key-bbbbbbbb"})
    assert r1.json()["id"] != r2.json()["id"]


def test_principal_id_does_not_leak_key_material(monkeypatch):
    _patch_settings(monkeypatch, api_keys="supersecretkey-ABC123")
    client = TestClient(_build_app())
    r = client.get("/user-principal", headers={"Authorization": "Bearer supersecretkey-ABC123"})
    assert r.status_code == 200
    pid = r.json()["id"]
    # No 6+-char substring from the raw key should appear in the id.
    raw = "supersecretkey-ABC123"
    for i in range(len(raw) - 5):
        chunk = raw[i : i + 6]
        assert chunk not in pid, f"id {pid!r} leaks raw-key chunk {chunk!r}"


def test_require_bridge_accepts_bridge_key(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/internal", headers={"Authorization": "Bearer bridge-secret-xxxxxxxx"})
    assert r.status_code == 200


def test_require_bridge_rejects_user_key(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/internal", headers={"Authorization": "Bearer user-key-aaaaaaaa"})
    assert r.status_code == 401


def test_require_bridge_rejects_missing_header(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    assert client.get("/internal").status_code == 401


def test_require_bridge_does_not_accept_query_string_token(monkeypatch):
    """The ?access_token= fallback is for browser <img>/<a> loads on
    user routes only. Internal callers always use the header."""
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/internal?access_token=bridge-secret-xxxxxxxx")
    assert r.status_code == 401


def test_require_user_accepts_bridge_key_when_flag_on(monkeypatch):
    _patch_settings(monkeypatch, allow_bridge_as_user=True)
    client = TestClient(_build_app())
    r = client.get("/user-principal", headers={"Authorization": "Bearer bridge-secret-xxxxxxxx"})
    assert r.status_code == 200
    assert r.json()["kind"] == "bridge"


def test_require_user_rejects_bridge_key_when_flag_off(monkeypatch):
    """H4 final state: once BEEVER_ALLOW_BRIDGE_AS_USER is flipped off,
    the bridge key must not be accepted on user-facing routes."""
    _patch_settings(monkeypatch, allow_bridge_as_user=False)
    client = TestClient(_build_app())
    r = client.get("/user", headers={"Authorization": "Bearer bridge-secret-xxxxxxxx"})
    assert r.status_code == 401


def test_query_string_user_auth_emits_audit_log(monkeypatch):
    _patch_settings(monkeypatch)
    calls: list[tuple[str, tuple]] = []

    def fake_info(msg, *args, **_kw):
        calls.append((msg, args))

    monkeypatch.setattr(auth_mod.logger, "info", fake_info)
    client = TestClient(_build_app())
    r = client.get("/user-principal?access_token=user-key-aaaaaaaa")
    assert r.status_code == 200
    audit_calls = [(m, a) for (m, a) in calls if "query_string_user" in m]
    assert audit_calls, "expected audit log when user key sent via query string"
    # The log must NOT carry the raw key material.
    for msg, args in audit_calls:
        rendered = msg % args if args else msg
        assert "user-key-aaaaaaaa" not in rendered


def test_principal_equals_string_of_id(monkeypatch):
    """str subclass contract: equality vs. the id string must hold so
    existing handlers that compare against a plain string keep working."""
    p = Principal("user:abc123def456", kind="user")
    assert p == "user:abc123def456"
    assert str(p) == "user:abc123def456"
    assert p.id == "user:abc123def456"
    assert p.kind == "user"


def test_no_keys_configured_still_rejects(monkeypatch):
    _patch_settings(monkeypatch, api_keys="", bridge_api_key="")
    client = TestClient(_build_app())
    r = client.get("/user", headers={"Authorization": "Bearer anything"})
    assert r.status_code == 401


# ── H4 final-state guarantees ───────────────────────────────────────────
#
# The `allow_bridge_as_user` default is False after Group 6. These tests
# lock in that default so the regression can't silently flip back.


def test_default_setting_rejects_bridge_as_user():
    """`Settings()` with no env override must default `allow_bridge_as_user` False."""
    from beever_atlas.infra.config import Settings

    s = Settings(api_keys="k", bridge_api_key="b")  # type: ignore[arg-type]
    assert s.allow_bridge_as_user is False, (
        "H4 regression: BEEVER_ALLOW_BRIDGE_AS_USER default must be False"
    )


def test_bridge_key_rejected_on_user_routes_with_default_config(monkeypatch):
    """End-to-end: bridge key → user route → 401 under default config."""
    # Intentionally do NOT pass allow_bridge_as_user — we want the default.
    base = dict(
        api_keys="user-key-aaaaaaaa",
        bridge_api_key="bridge-secret-xxxxxxxx",
        admin_token="admin-token-xyz",
    )

    def fake_get_settings():
        from beever_atlas.infra.config import Settings

        return Settings(**base)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mod, "get_settings", fake_get_settings)

    client = TestClient(_build_app())
    # User key → accepted.
    assert (
        client.get("/user", headers={"Authorization": "Bearer user-key-aaaaaaaa"}).status_code
        == 200
    )
    # Bridge key → rejected at the dependency layer.
    assert (
        client.get("/user", headers={"Authorization": "Bearer bridge-secret-xxxxxxxx"}).status_code
        == 401
    )


def test_emergency_override_still_works(monkeypatch):
    """The override path must remain functional for operators who need it."""
    _patch_settings(monkeypatch, allow_bridge_as_user=True)
    client = TestClient(_build_app())
    r = client.get("/user", headers={"Authorization": "Bearer bridge-secret-xxxxxxxx"})
    assert r.status_code == 200


@pytest.mark.parametrize("bad_header", ["", "Basic xyz", "Bearer", "Bearer  "])
def test_malformed_authorization_header(monkeypatch, bad_header):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/user", headers={"Authorization": bad_header} if bad_header else {})
    assert r.status_code == 401
