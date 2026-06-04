"""Tests for the typed Principal + split require_user / require_bridge deps.

Covers the `principal-auth` capability: stable ids, key-material hygiene,
user-vs-bridge kind filtering, and the transitional
``BEEVER_ALLOW_BRIDGE_AS_USER`` flag that closes security finding H4 when
flipped to False.
"""

from __future__ import annotations

from typing import Optional

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from beever_atlas.infra import auth as auth_mod
from beever_atlas.infra.auth import (
    Principal,
    require_bridge,
    require_user,
    require_user_loader,
    require_user_loader_optional,
)
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


def _build_loader_app() -> FastAPI:
    """A test app that mounts an endpoint with `require_user_loader`
    (header OR ?access_token=)."""
    app = FastAPI()

    @app.get("/loader", dependencies=[Depends(require_user_loader)])
    def loader_route():
        return {"ok": True}

    @app.get("/loader-principal")
    def loader_principal(p: Principal = Depends(require_user_loader)):
        return {"kind": p.kind, "id": p.id}

    return app


def _build_loader_optional_app() -> FastAPI:
    """A test app whose endpoint uses `require_user_loader_optional` —
    same auth surface as `require_user_loader` but returns None instead
    of 401 when auth is missing or invalid."""
    app = FastAPI()

    @app.get("/loader-optional")
    def loader_optional(p: Optional[Principal] = Depends(require_user_loader_optional)):
        return {"principal": None if p is None else {"kind": p.kind, "id": p.id}}

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
    """Issue #89 — when raw `?access_token=` falls through `require_user_loader`
    (the migration-window fallback path), the audit log line is
    `auth.loader_fallback_raw_key`. The old `auth.query_string_user` log
    rotated to this name in #89 because the semantic shifted from
    "query-string user auth" to "raw-key fallback exercised"."""
    _patch_settings(monkeypatch)
    calls: list[tuple[str, tuple]] = []

    def fake_info(msg, *args, **_kw):
        calls.append((msg, args))

    monkeypatch.setattr(auth_mod.logger, "info", fake_info)
    client = TestClient(_build_loader_app())
    r = client.get("/loader-principal?access_token=user-key-aaaaaaaa")
    assert r.status_code == 200
    audit_calls = [(m, a) for (m, a) in calls if "loader_fallback_raw_key" in m]
    assert audit_calls, (
        f"expected `auth.loader_fallback_raw_key` audit log when raw "
        f"?access_token= falls through; got {calls}"
    )
    # The log must NOT carry the raw key material.
    for msg, args in audit_calls:
        rendered = msg % args if args else msg
        assert "user-key-aaaaaaaa" not in rendered


# ── Issue #88: narrow ?access_token= surface to loader endpoints only ──


def test_require_user_rejects_query_string_only(monkeypatch):
    """`require_user` is header-only after #88. A request with only
    `?access_token=` must be rejected even when the key is otherwise valid."""
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/user?access_token=user-key-aaaaaaaa")
    assert r.status_code == 401


def test_require_user_still_accepts_header_auth(monkeypatch):
    """Header auth is unchanged — only the query-string fallback was removed."""
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/user", headers={"Authorization": "Bearer user-key-aaaaaaaa"})
    assert r.status_code == 200


def test_require_user_logs_rejected_query_string(monkeypatch):
    """When `?access_token=` is the SOLE auth, log so operators see the
    misconfigured caller."""
    _patch_settings(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(auth_mod.logger, "info", lambda msg, *a, **kw: calls.append(msg))
    client = TestClient(_build_app())
    client.get("/user?access_token=user-key-aaaaaaaa")  # 401, but log fires
    assert any("query_string_rejected" in c for c in calls), (
        f"expected auth.query_string_rejected log; got {calls}"
    )


def test_require_user_does_not_log_when_dual_auth(monkeypatch):
    """Caller presenting BOTH header AND query string is not relying on the
    query string — don't spam the rejection log."""
    _patch_settings(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(auth_mod.logger, "info", lambda msg, *a, **kw: calls.append(msg))
    client = TestClient(_build_app())
    client.get(
        "/user?access_token=user-key-aaaaaaaa",
        headers={"Authorization": "Bearer user-key-aaaaaaaa"},
    )
    assert not any("query_string_rejected" in c for c in calls)


def test_require_user_loader_accepts_query_string(monkeypatch):
    """The new loader dep accepts `?access_token=` for browser-native loaders."""
    _patch_settings(monkeypatch)
    client = TestClient(_build_loader_app())
    r = client.get("/loader?access_token=user-key-aaaaaaaa")
    assert r.status_code == 200


def test_require_user_loader_accepts_header_auth(monkeypatch):
    """The new loader dep also still accepts header auth (it's header OR query)."""
    _patch_settings(monkeypatch)
    client = TestClient(_build_loader_app())
    r = client.get("/loader", headers={"Authorization": "Bearer user-key-aaaaaaaa"})
    assert r.status_code == 200


def test_require_user_loader_optional_returns_none_on_missing_auth(monkeypatch):
    """`require_user_loader_optional` must return None (200, principal=None)
    when the request has neither header nor query-string auth — that's the
    contract the public shared-link endpoint relies on."""
    _patch_settings(monkeypatch)
    client = TestClient(_build_loader_optional_app())
    r = client.get("/loader-optional")
    assert r.status_code == 200
    assert r.json() == {"principal": None}


def test_require_user_loader_optional_resolves_query_string(monkeypatch):
    """Conversely, a valid `?access_token=` resolves to a Principal."""
    _patch_settings(monkeypatch)
    client = TestClient(_build_loader_optional_app())
    r = client.get("/loader-optional?access_token=user-key-aaaaaaaa")
    assert r.status_code == 200
    body = r.json()
    assert body["principal"] is not None
    assert body["principal"]["kind"] == "user"


def test_loader_endpoint_audit_guard():
    """AST-walk audit guard: only the documented loader-dep call sites
    should exist in `src/beever_atlas/api/`. A naive `text.count()` would
    over-trigger on comments/docstrings; we walk the AST and inspect the
    actual `Depends(...)` Call nodes plus direct invocations."""
    import ast
    import pathlib

    targets = {"require_user_loader", "require_user_loader_optional"}
    api_dir = pathlib.Path(__file__).resolve().parents[2] / "src" / "beever_atlas" / "api"
    assert api_dir.is_dir(), f"unexpected layout: {api_dir} not a directory"

    call_sites: list[str] = []
    for f in sorted(api_dir.glob("*.py")):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            # Direct call: require_user_loader_optional(...)
            if isinstance(node.func, ast.Name) and node.func.id in targets:
                call_sites.append(f"{f.name}:{node.lineno}:direct:{node.func.id}")
                continue
            # Wrapped call: Depends(require_user_loader)
            if isinstance(node.func, ast.Name) and node.func.id == "Depends":
                for arg in node.args:
                    if isinstance(arg, ast.Name) and arg.id in targets:
                        call_sites.append(f"{f.name}:{node.lineno}:Depends:{arg.id}")

    # Approved loader-dep call sites (the ?access_token= surface), keyed by file
    # so the guard tolerates line moves but fails loudly on any NEW site:
    #   ask.py:     1 direct `require_user_loader_optional(...)` — public shared-link endpoint.
    #   loaders.py: 2 `Depends(require_user_loader)` — proxy_file + proxy_media obtain the
    #               authenticated principal to enforce `assert_channel_access` on store hits
    #               (PR #226, S1 IDOR fix). These routes are ALREADY loader-mounted via
    #               app.py's `_loader_auth`, so reading the principal here does NOT widen the
    #               ?access_token= surface — it only closes a cross-channel media read.
    # If a developer adds a new loader endpoint / site, this fails LOUDLY so the
    # contributor must update this allow-list deliberately.
    from collections import Counter

    by_file = Counter(cs.split(":")[0] for cs in call_sites)
    assert by_file == {"ask.py": 1, "loaders.py": 2}, (
        f"Audit guard: loader-dep call sites changed: {dict(by_file)} :: {call_sites}. "
        "Issue #88: every new use of require_user_loader[_optional] expands the "
        "?access_token= surface. Update this allow-list deliberately if intended."
    )


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
