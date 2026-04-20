"""Unit tests for infra.channel_access.assert_connection_owned.

Tests:
- raises ConnectionAccessDenied when connection does not exist
- allows access when owner matches principal_id
- allows access for legacy:shared in single-tenant mode (user principal)
- denies access for bridge principal on legacy:shared rows
- denies access when owner is a different principal
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.errors import ConnectionAccessDenied
from beever_atlas.infra.auth import Principal
from beever_atlas.infra.channel_access import assert_connection_owned


def _make_conn(owner: str | None) -> MagicMock:
    conn = MagicMock()
    conn.owner_principal_id = owner
    return conn


def _mock_stores(conn):
    stores = MagicMock()
    stores.platform.get_connection = AsyncMock(return_value=conn)
    return stores


def _mock_settings(single_tenant: bool = True):
    settings = MagicMock()
    settings.beever_single_tenant = single_tenant
    return settings


@pytest.mark.asyncio
async def test_raises_when_connection_not_found():
    """assert_connection_owned raises ConnectionAccessDenied for missing connection."""
    stores = MagicMock()
    stores.platform.get_connection = AsyncMock(return_value=None)

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=stores),
        patch("beever_atlas.infra.channel_access.get_settings", return_value=_mock_settings()),
    ):
        with pytest.raises(ConnectionAccessDenied):
            await assert_connection_owned("user-A", "conn-missing")


@pytest.mark.asyncio
async def test_allows_explicit_owner_match():
    """assert_connection_owned allows when owner matches principal."""
    conn = _make_conn(owner="user-A")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch("beever_atlas.infra.channel_access.get_settings", return_value=_mock_settings()),
    ):
        await assert_connection_owned("user-A", "conn-1")  # should not raise


@pytest.mark.asyncio
async def test_allows_legacy_shared_for_user_in_single_tenant():
    """In single-tenant mode, user principals can access legacy:shared rows."""
    conn = _make_conn(owner="legacy:shared")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=True),
        ),
    ):
        # A bare string principal is treated as kind='user' per _principal_kind
        await assert_connection_owned("user-B", "conn-1")  # should not raise


@pytest.mark.asyncio
async def test_denies_owner_mismatch_in_single_tenant():
    """assert_connection_owned denies when owner is a different user (non-legacy)."""
    conn = _make_conn(owner="user-B")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch("beever_atlas.infra.channel_access.get_settings", return_value=_mock_settings()),
    ):
        with pytest.raises(ConnectionAccessDenied):
            await assert_connection_owned("user-A", "conn-1")


@pytest.mark.asyncio
async def test_denies_legacy_shared_in_multitenant():
    """In multi-tenant mode, legacy:shared rows are not accessible to non-owners."""
    conn = _make_conn(owner="legacy:shared")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=False),
        ),
    ):
        with pytest.raises(ConnectionAccessDenied):
            await assert_connection_owned("user-A", "conn-1")


# --- MCP-principal fallback (the new behavior) -------------------------------


@pytest.mark.asyncio
async def test_allows_legacy_shared_for_mcp_in_single_tenant():
    """MCP keys mirror user keys in single-tenant mode for the legacy fallback.

    Without this, list_connections (which already admits the fallback)
    and list_channels disagree, leaving MCP clients with empty channel
    lists despite list_connections returning the connection.
    """
    conn = _make_conn(owner="legacy:shared")
    mcp_principal = Principal("mcp:abc123", kind="mcp")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=True),
        ),
    ):
        await assert_connection_owned(mcp_principal, "conn-1")  # should not raise


@pytest.mark.asyncio
async def test_allows_unowned_for_mcp_in_single_tenant():
    """`owner_principal_id is None` is treated the same as legacy:shared."""
    conn = _make_conn(owner=None)
    mcp_principal = Principal("mcp:abc123", kind="mcp")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=True),
        ),
    ):
        await assert_connection_owned(mcp_principal, "conn-1")  # should not raise


@pytest.mark.asyncio
async def test_denies_legacy_shared_for_mcp_in_multitenant():
    """Multi-tenant mode is the security boundary: MCP keys must own each
    connection explicitly. The legacy fallback is single-tenant only."""
    conn = _make_conn(owner="legacy:shared")
    mcp_principal = Principal("mcp:abc123", kind="mcp")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=False),
        ),
    ):
        with pytest.raises(ConnectionAccessDenied):
            await assert_connection_owned(mcp_principal, "conn-1")


@pytest.mark.asyncio
async def test_bridge_principal_unchanged_no_legacy_fallback():
    """Regression guard: bridge principals stay strict in every mode.

    Bridges can be cross-tenant by design and must always carry an
    explicit `owner_principal_id` match. Loosening this would let any
    bridge token reach legacy/un-owned rows.
    """
    conn = _make_conn(owner="legacy:shared")
    bridge_principal = Principal("bridge", kind="bridge")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=True),
        ),
    ):
        with pytest.raises(ConnectionAccessDenied):
            await assert_connection_owned(bridge_principal, "conn-1")


# ----------------------------------------------------------------------
# MCP single-tenant fallback for user-owned rows (RES-232).
# Dashboard-created connections stamp ``owner_principal_id`` with a user
# principal id (``user:<hash>``); MCP must reach them in single-tenant
# mode because the MCP api-key represents the same operator.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_allows_user_owned_for_mcp_in_single_tenant():
    """MCP principal can access a user-owned connection in single-tenant mode."""
    conn = _make_conn(owner="user:abc123")
    mcp_principal = Principal("mcp:xyz789", kind="mcp")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=True),
        ),
    ):
        await assert_connection_owned(mcp_principal, "conn-1")  # should not raise


@pytest.mark.asyncio
async def test_denies_user_owned_for_mcp_in_multi_tenant():
    """Regression guard: in multi-tenant, MCP cannot reach user-owned rows without explicit match."""
    conn = _make_conn(owner="user:abc123")
    mcp_principal = Principal("mcp:xyz789", kind="mcp")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=False),
        ),
    ):
        with pytest.raises(ConnectionAccessDenied):
            await assert_connection_owned(mcp_principal, "conn-1")


@pytest.mark.asyncio
async def test_user_principal_denied_other_user_row_in_single_tenant():
    """Regression guard: a user principal must not inherit another user's row.

    The MCP fallback widens inheritance for MCP only — user principals keep
    their existing strict behaviour (``owner in {None, "legacy:shared"}``).
    """
    conn = _make_conn(owner="user:bbbb")
    user_a = Principal("user:aaaa", kind="user")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=True),
        ),
    ):
        with pytest.raises(ConnectionAccessDenied):
            await assert_connection_owned(user_a, "conn-1")


@pytest.mark.asyncio
async def test_bridge_denied_user_owned_in_single_tenant():
    """Regression guard: bridge principals still never inherit in single-tenant mode."""
    conn = _make_conn(owner="user:abc123")
    bridge_principal = Principal("bridge", kind="bridge")

    with (
        patch("beever_atlas.infra.channel_access.get_stores", return_value=_mock_stores(conn)),
        patch(
            "beever_atlas.infra.channel_access.get_settings",
            return_value=_mock_settings(single_tenant=True),
        ),
    ):
        with pytest.raises(ConnectionAccessDenied):
            await assert_connection_owned(bridge_principal, "conn-1")
