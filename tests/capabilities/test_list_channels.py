"""Unit tests for capabilities.connections.list_channels.

Tests:
- ConnectionAccessDenied is raised when the principal doesn't own the connection
- list_channels returns channel dicts for selected_channels when owned
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.capabilities.connections import list_channels
from beever_atlas.capabilities.errors import ConnectionAccessDenied


@pytest.mark.asyncio
async def test_list_channels_raises_when_not_owned():
    """list_channels raises ConnectionAccessDenied when assert_connection_owned denies access."""
    with patch(
        "beever_atlas.capabilities.connections.assert_connection_owned",
        side_effect=ConnectionAccessDenied("conn-X"),
    ):
        with pytest.raises(ConnectionAccessDenied) as exc_info:
            await list_channels("user-A", "conn-X")

    assert exc_info.value.connection_id == "conn-X"


@pytest.mark.asyncio
async def test_list_channels_returns_selected_channels_when_owned():
    """list_channels returns one dict per selected channel when the connection is owned."""
    conn = MagicMock()
    conn.id = "conn-1"
    conn.platform = "slack"
    conn.selected_channels = ["C1", "C2"]

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)

    with patch(
        "beever_atlas.capabilities.connections.assert_connection_owned",
        return_value=None,
    ), patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores):
        result = await list_channels("user-A", "conn-1")

    assert len(result) == 2
    channel_ids = {r["channel_id"] for r in result}
    assert channel_ids == {"C1", "C2"}
    assert all(r["platform"] == "slack" for r in result)


@pytest.mark.asyncio
async def test_list_channels_empty_selected():
    """list_channels returns empty list when no channels are selected."""
    conn = MagicMock()
    conn.id = "conn-1"
    conn.platform = "slack"
    conn.selected_channels = []

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)

    with patch(
        "beever_atlas.capabilities.connections.assert_connection_owned",
        return_value=None,
    ), patch("beever_atlas.capabilities.connections.get_stores", return_value=mock_stores):
        result = await list_channels("user-A", "conn-1")

    assert result == []
