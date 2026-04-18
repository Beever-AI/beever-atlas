"""Unit tests for capabilities.connections.list_connections.

Tests:
- owned connections are returned
- unowned connections are hidden (multi-tenant mode)
- single-tenant sentinel (legacy:shared) connections are visible to any user
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.connections import list_connections


def _make_conn(
    conn_id: str,
    platform: str = "slack",
    owner: str | None = None,
    selected_channels: list[str] | None = None,
    source: str = "ui",
    status: str = "connected",
) -> MagicMock:
    conn = MagicMock()
    conn.id = conn_id
    conn.platform = platform
    conn.display_name = f"Display {conn_id}"
    conn.owner_principal_id = owner
    conn.selected_channels = selected_channels or []
    conn.source = source
    conn.status = status
    return conn


@pytest.mark.asyncio
async def test_owned_connection_returned():
    """A connection owned by the principal is returned."""
    conn = _make_conn("conn-1", owner="user-A")

    mock_stores = MagicMock()
    mock_stores.platform.list_connections = AsyncMock(return_value=[conn])

    with patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores), \
         patch("beever_atlas.capabilities.connections._is_single_tenant", return_value=True):
        result = await list_connections("user-A")

    assert len(result) == 1
    assert result[0]["connection_id"] == "conn-1"


@pytest.mark.asyncio
async def test_unowned_connection_hidden_in_multitenant():
    """In multi-tenant mode, a connection owned by another principal is hidden."""
    conn = _make_conn("conn-1", owner="user-B")

    mock_stores = MagicMock()
    mock_stores.platform.list_connections = AsyncMock(return_value=[conn])

    with patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores), \
         patch("beever_atlas.capabilities.connections._is_single_tenant", return_value=False):
        result = await list_connections("user-A")

    assert result == []


@pytest.mark.asyncio
async def test_legacy_shared_sentinel_visible_in_single_tenant():
    """In single-tenant mode, legacy:shared connections are visible to any user."""
    conn = _make_conn("conn-legacy", owner="legacy:shared")

    mock_stores = MagicMock()
    mock_stores.platform.list_connections = AsyncMock(return_value=[conn])

    with patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores), \
         patch("beever_atlas.capabilities.connections._is_single_tenant", return_value=True):
        result = await list_connections("user-A")

    assert len(result) == 1
    assert result[0]["connection_id"] == "conn-legacy"


@pytest.mark.asyncio
async def test_none_owner_visible_in_single_tenant():
    """In single-tenant mode, unowned (None) connections are visible."""
    conn = _make_conn("conn-none", owner=None)

    mock_stores = MagicMock()
    mock_stores.platform.list_connections = AsyncMock(return_value=[conn])

    with patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores), \
         patch("beever_atlas.capabilities.connections._is_single_tenant", return_value=True):
        result = await list_connections("user-A")

    assert len(result) == 1


@pytest.mark.asyncio
async def test_legacy_shared_hidden_in_multitenant():
    """In multi-tenant mode, legacy:shared connections are hidden from non-owners."""
    conn = _make_conn("conn-legacy", owner="legacy:shared")

    mock_stores = MagicMock()
    mock_stores.platform.list_connections = AsyncMock(return_value=[conn])

    with patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores), \
         patch("beever_atlas.capabilities.connections._is_single_tenant", return_value=False):
        result = await list_connections("user-A")

    assert result == []


@pytest.mark.asyncio
async def test_selected_channel_count_in_response():
    """selected_channel_count reflects the number of selected channels."""
    conn = _make_conn("conn-1", owner="user-A", selected_channels=["C1", "C2", "C3"])

    mock_stores = MagicMock()
    mock_stores.platform.list_connections = AsyncMock(return_value=[conn])

    with patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores), \
         patch("beever_atlas.capabilities.connections._is_single_tenant", return_value=True):
        result = await list_connections("user-A")

    assert result[0]["selected_channel_count"] == 3


@pytest.mark.asyncio
async def test_mixed_ownership_filtered_correctly():
    """Only owned and legacy connections are returned; foreign-owned ones are excluded."""
    owned = _make_conn("conn-owned", owner="user-A")
    legacy = _make_conn("conn-legacy", owner="legacy:shared")
    foreign = _make_conn("conn-foreign", owner="user-B")

    mock_stores = MagicMock()
    mock_stores.platform.list_connections = AsyncMock(return_value=[owned, legacy, foreign])

    with patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores), \
         patch("beever_atlas.capabilities.connections._is_single_tenant", return_value=True):
        result = await list_connections("user-A")

    ids = {r["connection_id"] for r in result}
    assert "conn-owned" in ids
    assert "conn-legacy" in ids
    assert "conn-foreign" not in ids
