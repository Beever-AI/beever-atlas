"""Authz tests for ``assert_channel_delete_access`` (delete-channel-v2 Wave 0).

The destructive delete path is STRICTER than the read path
(``assert_channel_access``): no orphan-permissive fallback. Coverage:

  * single-tenant: user / mcp allowed; bridge denied.
  * multi-tenant: connection-owner allowed; non-owner denied; orphan
    (no connection lists the channel) denied for non-admin, allowed for admin;
    admin override on an owned channel.

Mirrors the fixture style of ``tests/api/test_channel_access.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import beever_atlas.stores as stores_mod
from beever_atlas.infra import channel_access as channel_access_mod
from beever_atlas.infra.auth import Principal
from beever_atlas.infra.config import Settings
from beever_atlas.models.platform_connection import PlatformConnection


def _conn(*, connection_id: str, selected: list[str], owner: str | None) -> PlatformConnection:
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
    base = dict(api_keys="test-key", beever_single_tenant=True)
    base.update(overrides)

    def _fake_get_settings() -> Settings:
        return Settings(**base)  # type: ignore[arg-type]

    monkeypatch.setattr(channel_access_mod, "get_settings", _fake_get_settings)


# ---------------------------------------------------------------------------
# Single-tenant
# ---------------------------------------------------------------------------


async def test_single_tenant_user_allowed(monkeypatch):
    _install_fake_stores(monkeypatch, [])
    _force_settings(monkeypatch, beever_single_tenant=True)
    await channel_access_mod.assert_channel_delete_access(Principal("user:abc", kind="user"), "C1")


async def test_single_tenant_mcp_allowed(monkeypatch):
    _install_fake_stores(monkeypatch, [])
    _force_settings(monkeypatch, beever_single_tenant=True)
    await channel_access_mod.assert_channel_delete_access(Principal("mcp:xyz", kind="mcp"), "C1")


async def test_single_tenant_bridge_denied(monkeypatch):
    _install_fake_stores(monkeypatch, [])
    _force_settings(monkeypatch, beever_single_tenant=True)
    with pytest.raises(HTTPException) as ei:
        await channel_access_mod.assert_channel_delete_access(
            Principal("bridge", kind="bridge"), "C1"
        )
    assert ei.value.status_code == 403


# ---------------------------------------------------------------------------
# Multi-tenant
# ---------------------------------------------------------------------------


async def test_multi_tenant_owner_allowed(monkeypatch):
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="user:abc")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    await channel_access_mod.assert_channel_delete_access(Principal("user:abc", kind="user"), "C1")


async def test_multi_tenant_non_owner_denied(monkeypatch):
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="user:owner")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    with pytest.raises(HTTPException) as ei:
        await channel_access_mod.assert_channel_delete_access(
            Principal("user:intruder", kind="user"), "C1"
        )
    assert ei.value.status_code == 403


async def test_multi_tenant_orphan_non_admin_denied(monkeypatch):
    # No connection lists C-ORPHAN — it is an orphan channel.
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["OTHER"], owner="user:abc")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    with pytest.raises(HTTPException) as ei:
        await channel_access_mod.assert_channel_delete_access(
            Principal("user:abc", kind="user"), "C-ORPHAN"
        )
    assert ei.value.status_code == 403


async def test_multi_tenant_orphan_admin_allowed(monkeypatch):
    _install_fake_stores(monkeypatch, [])
    _force_settings(monkeypatch, beever_single_tenant=False)
    # The admin sentinel principal ("admin") may clean up orphans.
    await channel_access_mod.assert_channel_delete_access("admin", "C-ORPHAN")


async def test_multi_tenant_admin_override_on_owned_channel(monkeypatch):
    _install_fake_stores(
        monkeypatch,
        [_conn(connection_id="c1", selected=["C1"], owner="user:owner")],
    )
    _force_settings(monkeypatch, beever_single_tenant=False)
    # Admin may delete a channel owned by someone else.
    await channel_access_mod.assert_channel_delete_access("admin", "C1")
