"""Task 2.2a: Principal.kind='mcp' + channel_access strictness for MCP principals.

Covers the spec requirement "MCP principals are a first-class kind and do NOT
inherit user browsing fallbacks" from specs/mcp-auth/spec.md.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beever_atlas.infra.auth import (
    Principal,
    _match_mcp_key,
    _principal_id_for_mcp_key,
)
from beever_atlas.infra import channel_access as ca_mod
from beever_atlas.infra.channel_access import (
    _principal_kind,
    assert_channel_access,
)


# ---------------------------------------------------------------------------
# _principal_kind resolves MCP principals correctly
# ---------------------------------------------------------------------------


def test_principal_kind_from_typed_mcp_principal():
    p = Principal("mcp:abc123def456ab01", kind="mcp")
    assert _principal_kind(p) == "mcp"


def test_principal_kind_from_string_with_mcp_prefix():
    # String-principal fallback path — middleware may attach raw string ids
    # to ASGI scope.state, and downstream helpers must still route correctly.
    assert _principal_kind("mcp:abc123def456ab01") == "mcp"


def test_principal_kind_string_user_prefix_still_user():
    assert _principal_kind("user:abc123def456ab01") == "user"


def test_principal_kind_unknown_shape_defaults_user():
    # Backward compat: test conftests that pre-date typed principals send
    # raw strings through. Without a prefix match, default to user so older
    # single-tenant integration tests keep working.
    assert _principal_kind("some-arbitrary-id") == "user"


# ---------------------------------------------------------------------------
# MCP helpers in auth.py
# ---------------------------------------------------------------------------


def test_principal_id_for_mcp_key_has_mcp_prefix():
    pid = _principal_id_for_mcp_key("my-secret-key")
    assert pid.startswith("mcp:")
    assert len(pid) == len("mcp:") + 16


def test_match_mcp_key_returns_mcp_kind():
    principal = _match_mcp_key("alpha", ["alpha", "beta"])
    assert principal is not None
    assert principal.kind == "mcp"
    assert principal.id.startswith("mcp:")


def test_match_mcp_key_no_match_returns_none():
    assert _match_mcp_key("wrong", ["alpha", "beta"]) is None


def test_match_mcp_key_empty_keys_returns_none():
    assert _match_mcp_key("any-token", []) is None


# ---------------------------------------------------------------------------
# MCP principals are strict — no single-tenant browsing fallback
# ---------------------------------------------------------------------------


class _FakePlatformStore:
    def __init__(self, connections):
        self._conns = connections

    async def list_connections(self):
        return list(self._conns)


def _patch_channel_access(monkeypatch, connections, *, single_tenant: bool = True):
    """Stub out channel_access's stores + settings lookups."""
    fake_stores = SimpleNamespace(platform=_FakePlatformStore(connections))
    monkeypatch.setattr(ca_mod, "get_stores", lambda: fake_stores)

    fake_settings = SimpleNamespace(beever_single_tenant=single_tenant)
    monkeypatch.setattr(ca_mod, "get_settings", lambda: fake_settings)


def _conn(owner, selected):
    return SimpleNamespace(owner_principal_id=owner, selected_channels=selected)


@pytest.mark.asyncio
async def test_mcp_principal_denied_on_unowned_channel_in_single_tenant(monkeypatch):
    """User principals get a browsing fallback in single-tenant mode; MCP
    principals do NOT. This is the core strictness property we care about."""
    # No connection lists ch-x in selected_channels → "no matching" path.
    _patch_channel_access(monkeypatch, connections=[], single_tenant=True)
    from fastapi import HTTPException

    mcp_principal = Principal("mcp:abc123def456ab01", kind="mcp")
    with pytest.raises(HTTPException) as exc:
        await assert_channel_access(mcp_principal, "ch-x")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_user_principal_permitted_on_unowned_channel_in_single_tenant(monkeypatch):
    """Contrast with above: user principal gets the browsing fallback."""
    _patch_channel_access(monkeypatch, connections=[], single_tenant=True)

    user_principal = Principal("user:abc123def456ab01", kind="user")
    # Should NOT raise — single-tenant user browsing fallback applies.
    await assert_channel_access(user_principal, "ch-x")


@pytest.mark.asyncio
async def test_mcp_principal_permitted_on_owned_channel(monkeypatch):
    """Explicit ownership match permits any kind, including MCP."""
    owner_id = "mcp:abc123def456ab01"
    conn = _conn(owner=owner_id, selected=["ch-a"])
    _patch_channel_access(monkeypatch, connections=[conn], single_tenant=True)

    mcp_principal = Principal(owner_id, kind="mcp")
    # Should NOT raise.
    await assert_channel_access(mcp_principal, "ch-a")


@pytest.mark.asyncio
async def test_mcp_principal_denied_on_legacy_shared_channel(monkeypatch):
    """MCP principals never inherit the legacy:shared single-tenant fallback."""
    conn = _conn(owner="legacy:shared", selected=["ch-a"])
    _patch_channel_access(monkeypatch, connections=[conn], single_tenant=True)
    from fastapi import HTTPException

    mcp_principal = Principal("mcp:abc123def456ab01", kind="mcp")
    with pytest.raises(HTTPException) as exc:
        await assert_channel_access(mcp_principal, "ch-a")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_mcp_principal_denied_in_multitenant_unowned(monkeypatch):
    """In multi-tenant mode, MCP principals need explicit ownership."""
    conn = _conn(owner=None, selected=["ch-a"])
    _patch_channel_access(monkeypatch, connections=[conn], single_tenant=False)
    from fastapi import HTTPException

    mcp_principal = Principal("mcp:abc123def456ab01", kind="mcp")
    with pytest.raises(HTTPException) as exc:
        await assert_channel_access(mcp_principal, "ch-a")
    assert exc.value.status_code == 403
