"""Channel access-control tests (RES-177 H1).

Covers `channel-access-control/spec.md`:
- Owner principal can read their own channel.
- Non-owner principal is blocked with 403.
- Single-tenant fallback admits legacy sentinel rows.
- Multi-tenant mode blocks legacy sentinel rows for non-matching principal.
- Unknown channel (no connection includes it) → 403.
- Bridge-key DELETE on /api/channels/{id}/data:
    * `BEEVER_ALLOW_BRIDGE_AS_USER=true`  → passes auth, hits the guard, 403
    * `BEEVER_ALLOW_BRIDGE_AS_USER=false` → rejected at auth layer, 401
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

import beever_atlas.stores as stores_mod
from beever_atlas.infra import auth as auth_mod
from beever_atlas.infra import channel_access as channel_access_mod
from beever_atlas.infra.auth import Principal
from beever_atlas.infra.config import Settings
from beever_atlas.models.platform_connection import PlatformConnection


# ---------------------------------------------------------------------------
# Direct unit coverage of assert_channel_access (no FastAPI)
# ---------------------------------------------------------------------------


def _conn(
    *,
    connection_id: str,
    selected: list[str],
    owner: str | None,
) -> PlatformConnection:
    return PlatformConnection(
        id=connection_id,
        platform="slack",
        source="ui",
        display_name=connection_id,
        status="connected",
        selected_channels=selected,
        encrypted_credentials=b"",
        credential_iv=b"",
        credential_tag=b"",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        owner_principal_id=owner,
    )


def _install_fake_stores(monkeypatch, connections: list[PlatformConnection]):
    fake = MagicMock(name="MockStoreClients")
    fake.platform = MagicMock()
    fake.platform.list_connections = AsyncMock(return_value=list(connections))
    monkeypatch.setattr(stores_mod, "_stores", fake)
    return fake


def _force_settings(monkeypatch, **overrides):
    base = dict(
        api_keys="test-key",
        beever_single_tenant=True,
    )
    base.update(overrides)

    def _fake_get_settings() -> Settings:
        return Settings(**base)  # type: ignore[arg-type]

    monkeypatch.setattr(channel_access_mod, "get_settings", _fake_get_settings)


@pytest.mark.asyncio
async def test_owner_principal_allowed_on_own_channel(monkeypatch):
    owner = Principal("user:abc", kind="user")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="user:abc")],
    )
    _force_settings(monkeypatch)
    # Should not raise.
    await channel_access_mod.assert_channel_access(owner, "C1")


@pytest.mark.asyncio
async def test_non_owner_principal_blocked_with_403(monkeypatch):
    from fastapi import HTTPException

    other = Principal("user:xyz", kind="user")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="user:abc")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    with pytest.raises(HTTPException) as exc:
        await channel_access_mod.assert_channel_access(other, "C1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_single_tenant_fallback_admits_legacy_shared(monkeypatch):
    """`BEEVER_SINGLE_TENANT=true` + legacy:shared owner → any user allowed."""
    any_user = Principal("user:some-new-user", kind="user")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="legacy:shared")],
    )
    _force_settings(monkeypatch, beever_single_tenant=True)
    await channel_access_mod.assert_channel_access(any_user, "C1")


@pytest.mark.asyncio
async def test_multi_tenant_blocks_legacy_shared_for_non_matching(monkeypatch):
    from fastapi import HTTPException

    any_user = Principal("user:some-new-user", kind="user")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="legacy:shared")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    with pytest.raises(HTTPException) as exc:
        await channel_access_mod.assert_channel_access(any_user, "C1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_single_tenant_fallback_admits_missing_owner(monkeypatch):
    """A row that predates the migration and has owner=None still works
    under single-tenant fallback (the backfill runs on the next boot)."""
    any_user = Principal("user:some-new-user", kind="user")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner=None)],
    )
    _force_settings(monkeypatch, beever_single_tenant=True)
    await channel_access_mod.assert_channel_access(any_user, "C1")


@pytest.mark.asyncio
async def test_unclaimed_channel_allowed_in_single_tenant(monkeypatch):
    """`selected_channels` is a sync pick-list, not an access ACL. A
    single-tenant user principal must be able to browse / pre-sync a
    channel that no connection has yet added to its pick-list —
    otherwise the UI can't discover new Slack/Discord channels."""
    user = Principal("user:abc", kind="user")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["OTHER"], owner="user:abc")],
    )
    _force_settings(monkeypatch)  # single-tenant (default)
    # Must not raise.
    await channel_access_mod.assert_channel_access(user, "C1")


@pytest.mark.asyncio
async def test_unclaimed_channel_rejected_in_multi_tenant(monkeypatch):
    """Multi-tenant mode stays strict: unclaimed channels are unreachable
    until an operator adds them to an owned connection's selected_channels."""
    from fastapi import HTTPException

    user = Principal("user:abc", kind="user")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["OTHER"], owner="user:abc")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    with pytest.raises(HTTPException) as exc:
        await channel_access_mod.assert_channel_access(user, "C1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_unclaimed_channel_rejected_for_bridge_even_in_single_tenant(monkeypatch):
    """Bridge principals never inherit the single-tenant browsing grace
    — the browsing UX is for users, not the internal bridge."""
    from fastapi import HTTPException

    bridge = Principal("bridge", kind="bridge")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["OTHER"], owner="user:abc")],
    )
    _force_settings(monkeypatch, beever_single_tenant=True)
    with pytest.raises(HTTPException) as exc:
        await channel_access_mod.assert_channel_access(bridge, "C1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bridge_principal_without_ownership_is_blocked(monkeypatch):
    """Bridge principals don't own user channels under the default policy."""
    from fastapi import HTTPException

    bridge = Principal("bridge", kind="bridge")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="legacy:shared")],
    )
    # Even in single-tenant mode, bridge kind is NOT user and must not
    # inherit the legacy fallback.
    _force_settings(monkeypatch, beever_single_tenant=True)
    with pytest.raises(HTTPException) as exc:
        await channel_access_mod.assert_channel_access(bridge, "C1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_bare_string_principal_treated_as_user(monkeypatch):
    """Legacy tests/conftests that still hand a bare str keep working."""
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="legacy:shared")],
    )
    _force_settings(monkeypatch, beever_single_tenant=True)
    await channel_access_mod.assert_channel_access("user:legacy-caller", "C1")


# ---------------------------------------------------------------------------
# MCP principal fallback (single-tenant = MCP api-key represents the user)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_principal_admitted_on_legacy_channel_single_tenant(monkeypatch):
    """Without this, every retrieval / graph / wiki tool from MCP hits 403
    on legacy-owned channels — the exact `channel_access_denied` the user
    reported on `tech-beever-atlas` after the list-side fix landed.
    """
    mcp = Principal("mcp:abc123", kind="mcp")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="legacy:shared")],
    )
    _force_settings(monkeypatch, beever_single_tenant=True)
    await channel_access_mod.assert_channel_access(mcp, "C1")  # must not raise


@pytest.mark.asyncio
async def test_mcp_principal_admitted_on_unclaimed_channel_single_tenant(monkeypatch):
    """Browsing grace applies to MCP as well — a channel the user hasn't
    added to `selected_channels` is still reachable via MCP when the
    underlying connection is single-tenant-owned."""
    mcp = Principal("mcp:abc123", kind="mcp")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["OTHER"], owner="user:some-owner")],
    )
    _force_settings(monkeypatch, beever_single_tenant=True)
    await channel_access_mod.assert_channel_access(mcp, "C1")  # must not raise


@pytest.mark.asyncio
async def test_mcp_principal_rejected_on_legacy_channel_multi_tenant(monkeypatch):
    """Multi-tenant mode is the real security boundary. MCP keys must
    explicitly own each connection they read from — no legacy fallback."""
    from fastapi import HTTPException

    mcp = Principal("mcp:abc123", kind="mcp")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="legacy:shared")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    with pytest.raises(HTTPException) as exc:
        await channel_access_mod.assert_channel_access(mcp, "C1")
    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_mcp_principal_with_explicit_ownership_always_allowed(monkeypatch):
    """The explicit-owner branch is unchanged — MCP principals whose id
    matches `owner_principal_id` pass in either single- or multi-tenant."""
    mcp = Principal("mcp:owned-by-me", kind="mcp")
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="mcp:owned-by-me")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    await channel_access_mod.assert_channel_access(mcp, "C1")  # must not raise


@pytest.mark.asyncio
async def test_bare_mcp_string_principal_detected_by_prefix(monkeypatch):
    """Handlers pass a bare `principal_id` string (not a Principal object)
    into the capability layer. `_principal_kind` must detect the `mcp:`
    prefix so the fallback still applies."""
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="legacy:shared")],
    )
    _force_settings(monkeypatch, beever_single_tenant=True)
    await channel_access_mod.assert_channel_access("mcp:bare-string", "C1")


# ---------------------------------------------------------------------------
# Integration: bridge key on DELETE /api/channels/{id}/data
# ---------------------------------------------------------------------------


def _patch_auth_settings(monkeypatch, **overrides):
    base = dict(
        api_keys="user-key-alpha",
        bridge_api_key="bridge-secret-zzz",
        admin_token="admin-tok",
        allow_bridge_as_user=True,
    )
    base.update(overrides)

    def _fake_get_settings() -> Settings:
        return Settings(**base)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mod, "get_settings", _fake_get_settings)


def test_bridge_key_delete_blocked_by_guard_when_flag_on(monkeypatch):
    """With `BEEVER_ALLOW_BRIDGE_AS_USER=true`, a bridge token passes
    `require_user` (returning a bridge principal) and then hits
    `_assert_channel_access`, which denies it with 403."""
    from fastapi.testclient import TestClient

    from beever_atlas.server.app import app

    # Disable the conftest fake-user override — we want the real dependency.
    from beever_atlas.infra.auth import require_user

    app.dependency_overrides.pop(require_user, None)

    _patch_auth_settings(monkeypatch, allow_bridge_as_user=True)
    _force_settings(monkeypatch, beever_single_tenant=True)
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="user:some-real-owner")],
    )

    client = TestClient(app)
    r = client.delete(
        "/api/channels/C1/data",
        headers={"Authorization": "Bearer bridge-secret-zzz"},
    )
    # Auth accepts the bridge key (flag on), but the channel-access guard
    # denies because "bridge" principal doesn't own any user channel.
    assert r.status_code == 403, r.text


def test_bridge_key_delete_rejected_at_auth_when_flag_off(monkeypatch):
    """With the flag off, the bridge key never reaches the guard — 401."""
    from fastapi.testclient import TestClient

    from beever_atlas.server.app import app
    from beever_atlas.infra.auth import require_user

    app.dependency_overrides.pop(require_user, None)

    _patch_auth_settings(monkeypatch, allow_bridge_as_user=False)
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="user:some-real-owner")],
    )

    client = TestClient(app)
    r = client.delete(
        "/api/channels/C1/data",
        headers={"Authorization": "Bearer bridge-secret-zzz"},
    )
    assert r.status_code == 401


def test_non_owner_user_blocked_on_delete(monkeypatch):
    """A user key bound to a principal that doesn't own C1 → 403."""
    from fastapi.testclient import TestClient

    from beever_atlas.server.app import app
    from beever_atlas.infra.auth import require_user

    app.dependency_overrides.pop(require_user, None)

    _patch_auth_settings(monkeypatch)
    _force_settings(monkeypatch, beever_single_tenant=False)
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="user:some-real-owner")],
    )

    client = TestClient(app)
    r = client.delete(
        "/api/channels/C1/data",
        headers={"Authorization": "Bearer user-key-alpha"},
    )
    assert r.status_code == 403
