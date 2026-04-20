"""Task 2.8: end-to-end auth matrix for the MCP ASGI middleware.

Verifies the MCPAuthMiddleware contract from specs/mcp-auth/spec.md:
- Missing/invalid/cross-realm tokens → 401
- Valid MCP bearer → passes to inner app
- Query-string credentials rejected regardless of header state
- Principal id + kind + request id attached to ASGI scope.state
- Authorization header stripped from scope before reaching inner app

Uses Starlette directly rather than mounting FastMCP so the test targets the
auth boundary in isolation — FastMCP protocol behaviour is exercised
separately in Phase 3 integration tests.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route

from beever_atlas.infra import mcp_auth as mcp_auth_mod
from beever_atlas.infra.mcp_auth import MCPAuthMiddleware, _reset_watchdog_for_tests


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


async def _stub_tool_handler(request):
    """Echo back the principal id + whether Authorization leaked."""
    state = request.scope.get("state") or {}
    # The middleware must strip Authorization from the scope headers before
    # forwarding — prove it here.
    auth_header_present = any(
        name.lower() == b"authorization" for name, _ in request.scope.get("headers", [])
    )
    return JSONResponse(
        {
            "principal_id": state.get("mcp_principal_id"),
            "principal_kind": state.get("mcp_principal_kind"),
            "request_id": state.get("mcp_request_id"),
            "auth_header_present": auth_header_present,
        }
    )


def _build_app():
    return Starlette(
        routes=[Route("/anything", _stub_tool_handler, methods=["GET", "POST"])],
        middleware=[Middleware(MCPAuthMiddleware)],
    )


def _patch_settings(monkeypatch, *, mcp_keys: str = "testmcp-1,testmcp-2"):
    fake = SimpleNamespace(beever_mcp_api_keys=mcp_keys)
    monkeypatch.setattr(mcp_auth_mod, "get_settings", lambda: fake)
    _reset_watchdog_for_tests()


# ---------------------------------------------------------------------------
# Auth matrix
# ---------------------------------------------------------------------------


def test_missing_authorization_returns_401(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/anything")
    assert r.status_code == 401
    assert r.headers.get("www-authenticate", "").lower().startswith("bearer")
    body = r.json()
    assert body["error"] == "mcp_unauthorized"
    assert body["reason"] == "missing_bearer"


def test_malformed_authorization_returns_401(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/anything", headers={"Authorization": "NotBearer xyz"})
    assert r.status_code == 401
    assert r.json()["reason"] == "missing_bearer"


def test_invalid_bearer_returns_401(monkeypatch):
    _patch_settings(monkeypatch)
    client = TestClient(_build_app())
    r = client.get("/anything", headers={"Authorization": "Bearer not-a-real-key"})
    assert r.status_code == 401
    assert r.json()["reason"] == "invalid_bearer"


def test_user_key_rejected_at_mcp(monkeypatch):
    # User key (would work on /api/*) is NOT in beever_mcp_api_keys.
    _patch_settings(monkeypatch, mcp_keys="mcp-only-key")
    client = TestClient(_build_app())
    r = client.get("/anything", headers={"Authorization": "Bearer user-key-aaaa"})
    assert r.status_code == 401
    assert r.json()["reason"] == "invalid_bearer"


def test_bridge_key_rejected_at_mcp(monkeypatch):
    _patch_settings(monkeypatch, mcp_keys="mcp-only-key")
    client = TestClient(_build_app())
    r = client.get("/anything", headers={"Authorization": "Bearer bridge-secret-xxxx"})
    assert r.status_code == 401


def test_valid_mcp_key_passes_through(monkeypatch):
    _patch_settings(monkeypatch, mcp_keys="testmcp-abc")
    client = TestClient(_build_app())
    r = client.get("/anything", headers={"Authorization": "Bearer testmcp-abc"})
    assert r.status_code == 200
    body = r.json()
    assert body["principal_kind"] == "mcp"
    assert body["principal_id"].startswith("mcp:")
    assert body["auth_header_present"] is False, (
        "Authorization header MUST be stripped before reaching the inner app"
    )
    assert body["request_id"] is not None and len(body["request_id"]) > 0


def test_query_string_access_token_rejected(monkeypatch):
    """MCP middleware MUST NOT accept ?access_token= fallback."""
    _patch_settings(monkeypatch, mcp_keys="testmcp-abc")
    client = TestClient(_build_app())
    r = client.get("/anything?access_token=testmcp-abc")
    assert r.status_code == 401
    assert r.json()["reason"] == "query_string_credentials_not_allowed"


def test_query_string_api_key_rejected(monkeypatch):
    _patch_settings(monkeypatch, mcp_keys="testmcp-abc")
    client = TestClient(_build_app())
    r = client.get(
        "/anything?api_key=testmcp-abc",
        headers={"Authorization": "Bearer testmcp-abc"},  # still rejected
    )
    assert r.status_code == 401
    assert r.json()["reason"] == "query_string_credentials_not_allowed"


def test_no_mcp_keys_configured_returns_401(monkeypatch):
    """If MCP is not configured, the middleware should refuse cleanly."""
    _patch_settings(monkeypatch, mcp_keys="")
    client = TestClient(_build_app())
    r = client.get("/anything", headers={"Authorization": "Bearer any-token"})
    assert r.status_code == 401
    assert r.json()["reason"] == "no_mcp_keys_configured"


# ---------------------------------------------------------------------------
# Brute-force watchdog (task 2.3)
# ---------------------------------------------------------------------------


def test_bruteforce_watchdog_triggers_after_five_failures(monkeypatch):
    """Watchdog must record an alert timestamp after crossing the 5-in-60s
    threshold. Inspecting the internal ``_ip_last_alert`` map is more robust
    than log-capture assertions (the app's structured formatter bypasses
    caplog)."""
    _patch_settings(monkeypatch)
    # Sanity: empty at start.
    assert mcp_auth_mod._ip_last_alert == {}
    client = TestClient(_build_app())
    for _ in range(6):
        client.get("/anything", headers={"Authorization": "Bearer bad-token"})
    # After 5+ failures in window, an alert must have fired for this ip.
    assert "testclient" in mcp_auth_mod._ip_last_alert, (
        f"Expected bruteforce alert for 'testclient'; got state={mcp_auth_mod._ip_last_alert!r}"
    )
    # And the failure counter must have recorded all 6 events.
    assert len(mcp_auth_mod._ip_failures.get("testclient", [])) >= 5


def test_bruteforce_watchdog_does_not_trigger_on_few_failures(monkeypatch):
    _patch_settings(monkeypatch)
    _reset_watchdog_for_tests()
    client = TestClient(_build_app())
    for _ in range(3):
        client.get("/anything", headers={"Authorization": "Bearer bad-token"})
    # Only 3 failures: below threshold — no alert.
    assert "testclient" not in mcp_auth_mod._ip_last_alert


# ---------------------------------------------------------------------------
# Disjoint-key assertion at boot (task 2.2a / D2 adjustment)
# ---------------------------------------------------------------------------


def test_disjoint_key_assertion_rejects_overlapping_keys():
    """MCP keys must not appear in BEEVER_API_KEYS or BRIDGE_API_KEY."""
    from beever_atlas.infra.config import validate_keys_disjoint

    import pytest as _pytest

    # Overlap user ↔ mcp
    with _pytest.raises(ValueError):
        validate_keys_disjoint(
            api_keys="shared-key",
            bridge_api_key="bridge-ok",
            mcp_api_keys="shared-key",
        )

    # Overlap bridge ↔ mcp
    with _pytest.raises(ValueError):
        validate_keys_disjoint(
            api_keys="user-ok",
            bridge_api_key="shared-key",
            mcp_api_keys="shared-key",
        )

    # Disjoint — OK
    validate_keys_disjoint(
        api_keys="user-1,user-2",
        bridge_api_key="bridge-secret",
        mcp_api_keys="mcp-1,mcp-2",
    )
