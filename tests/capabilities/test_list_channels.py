"""Unit tests for capabilities.connections.list_channels.

The capability delegates channel discovery to
``beever_atlas.services.channel_discovery.fetch_connection_channels_safe``
(the same helper the dashboard uses). These tests patch that helper
directly so no real bridge / adapter is exercised.

Scenarios covered:
- ConnectionAccessDenied when the principal doesn't own the connection
- Non-file connections return the full bridge list (not just
  ``selected_channels``)
- ``selected_channels`` acts as a filter only when non-empty (pick-list,
  not ACL)
- File connections resolve to activity-log display names and report
  ``sync_status="n/a"``
- Sync state is populated from ``get_channel_sync_states_batch``
- Graceful degradation when the sync-state lookup fails
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.adapters import ChannelInfo
from beever_atlas.capabilities.connections import list_channels
from beever_atlas.capabilities.errors import ConnectionAccessDenied


def _ch(
    channel_id: str,
    name: str,
    platform: str = "slack",
    is_member: bool = True,
) -> ChannelInfo:
    return ChannelInfo(
        channel_id=channel_id,
        name=name,
        platform=platform,
        is_member=is_member,
        connection_id="conn-1",
    )


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
async def test_returns_member_channels_when_selected_empty():
    """When selected_channels is empty, only is_member=True channels surface.

    The capability must ask the discovery service for is_member-filtered
    results (``is_member_only=True``) so the QA agent only sees channels
    the bot can actually read messages from. Non-member channels — which
    the dashboard's Channels page shows as "AVAILABLE" — must be
    excluded from the MCP/orchestration path.
    """
    conn = MagicMock()
    conn.id = "conn-1"
    conn.platform = "slack"
    conn.selected_channels = []

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)
    mock_stores.mongodb.get_channel_sync_states_batch = AsyncMock(return_value={})

    # Mix of member and non-member channels. The discovery mock respects
    # is_member_only=True and returns only the members; the capability
    # must forward the flag so this filtering actually happens.
    discovery_result = [
        _ch("C1", "general", is_member=True),
        _ch("C3", "announcements", is_member=True),
    ]

    with (
        patch(
            "beever_atlas.capabilities.connections.assert_connection_owned",
            return_value=None,
        ),
        patch(
            "beever_atlas.capabilities.connections.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.capabilities.connections.fetch_connection_channels_safe",
            AsyncMock(return_value=discovery_result),
        ) as fetch_mock,
    ):
        result = await list_channels("user-A", "conn-1")

    # Capability forwards is_member_only=True to the discovery service.
    fetch_mock.assert_awaited_once_with(
        "conn-1",
        [],
        "slack",
        is_member_only=True,
    )
    assert {r["channel_id"] for r in result} == {"C1", "C3"}
    assert {r["name"] for r in result} == {"general", "announcements"}


@pytest.mark.asyncio
async def test_selected_channels_trumps_is_member_filter():
    """When selected_channels is non-empty, the pick-list wins.

    Even if some channels in the pick-list have ``is_member=False``, the
    user has explicitly opted in, so they must be returned. The
    capability still passes ``is_member_only=True`` to the discovery
    service (which ignores it when ``selected`` is non-empty).
    """
    conn = MagicMock()
    conn.id = "conn-1"
    conn.platform = "slack"
    conn.selected_channels = ["C1", "C2", "C3"]

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)
    mock_stores.mongodb.get_channel_sync_states_batch = AsyncMock(return_value={})

    # Mix of member and non-member channels — all in the selected set.
    discovery_result = [
        _ch("C1", "general", is_member=True),
        _ch("C2", "private-no-bot", is_member=False),
        _ch("C3", "announcements", is_member=False),
    ]

    with (
        patch(
            "beever_atlas.capabilities.connections.assert_connection_owned",
            return_value=None,
        ),
        patch(
            "beever_atlas.capabilities.connections.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.capabilities.connections.fetch_connection_channels_safe",
            AsyncMock(return_value=discovery_result),
        ) as fetch_mock,
    ):
        result = await list_channels("user-A", "conn-1")

    fetch_mock.assert_awaited_once_with(
        "conn-1",
        ["C1", "C2", "C3"],
        "slack",
        is_member_only=True,
    )
    # All three returned — the explicit pick-list wins over is_member.
    assert {r["channel_id"] for r in result} == {"C1", "C2", "C3"}


@pytest.mark.asyncio
async def test_list_channels_passes_selected_filter_to_service():
    """When selected_channels is non-empty, the capability forwards it verbatim.

    Filtering is the discovery service's responsibility; the capability
    only decorates the returned list with sync state.
    """
    conn = MagicMock()
    conn.id = "conn-1"
    conn.platform = "discord"
    conn.selected_channels = ["CH1", "CH2"]

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)
    mock_stores.mongodb.get_channel_sync_states_batch = AsyncMock(return_value={})

    # Service returns whatever it wants; capability trusts it.
    discovery_result = [_ch("CH1", "alpha", "discord")]

    with (
        patch(
            "beever_atlas.capabilities.connections.assert_connection_owned",
            return_value=None,
        ),
        patch(
            "beever_atlas.capabilities.connections.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.capabilities.connections.fetch_connection_channels_safe",
            AsyncMock(return_value=discovery_result),
        ) as fetch_mock,
    ):
        result = await list_channels("user-A", "conn-1")

    fetch_mock.assert_awaited_once_with(
        "conn-1",
        ["CH1", "CH2"],
        "discord",
        is_member_only=True,
    )
    assert len(result) == 1
    assert result[0]["channel_id"] == "CH1"


@pytest.mark.asyncio
async def test_list_channels_file_connection_sync_na():
    """File-type connections report sync_status='n/a' and skip sync-state lookup."""
    conn = MagicMock()
    conn.id = "conn-file"
    conn.platform = "file"
    conn.selected_channels = ["file-abc", "file-xyz"]

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)
    # Intentionally un-stubbed — a call would raise because MagicMock returns
    # non-awaitable MagicMocks, guarding against a wrong sync-state query.

    discovery_result = [
        _ch("file-abc", "notes.pdf", "file"),
        _ch("file-xyz", "minutes.docx", "file"),
    ]

    with (
        patch(
            "beever_atlas.capabilities.connections.assert_connection_owned",
            return_value=None,
        ),
        patch(
            "beever_atlas.capabilities.connections.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.capabilities.connections.fetch_connection_channels_safe",
            AsyncMock(return_value=discovery_result),
        ),
    ):
        result = await list_channels("user-A", "conn-file")

    assert len(result) == 2
    for row in result:
        assert row["platform"] == "file"
        assert row["sync_status"] == "n/a"
        assert row["last_sync_ts"] is None
        assert row["message_count_estimate"] is None
    assert {r["name"] for r in result} == {"notes.pdf", "minutes.docx"}


@pytest.mark.asyncio
async def test_list_channels_populates_sync_state_when_available():
    """For non-file connections, sync state decorates each channel dict."""
    conn = MagicMock()
    conn.id = "conn-1"
    conn.platform = "slack"
    conn.selected_channels = []  # browse-all mode

    state = MagicMock()
    state.last_sync_ts = "2026-04-18T12:00:00Z"
    state.total_synced_messages = 42

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)
    mock_stores.mongodb.get_channel_sync_states_batch = AsyncMock(
        return_value={"C1": state},
    )

    discovery_result = [_ch("C1", "general"), _ch("C2", "random")]

    with (
        patch(
            "beever_atlas.capabilities.connections.assert_connection_owned",
            return_value=None,
        ),
        patch(
            "beever_atlas.capabilities.connections.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.capabilities.connections.fetch_connection_channels_safe",
            AsyncMock(return_value=discovery_result),
        ),
    ):
        result = await list_channels("user-A", "conn-1")

    by_id = {r["channel_id"]: r for r in result}
    assert by_id["C1"]["sync_status"] == "synced"
    assert by_id["C1"]["last_sync_ts"] == "2026-04-18T12:00:00Z"
    assert by_id["C1"]["message_count_estimate"] == 42
    assert by_id["C2"]["sync_status"] == "never_synced"
    assert by_id["C2"]["last_sync_ts"] is None


@pytest.mark.asyncio
async def test_list_channels_empty_when_service_returns_empty():
    """When the discovery service returns [], the capability returns []."""
    conn = MagicMock()
    conn.id = "conn-1"
    conn.platform = "slack"
    conn.selected_channels = []

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)

    with (
        patch(
            "beever_atlas.capabilities.connections.assert_connection_owned",
            return_value=None,
        ),
        patch(
            "beever_atlas.capabilities.connections.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.capabilities.connections.fetch_connection_channels_safe",
            AsyncMock(return_value=[]),
        ),
    ):
        result = await list_channels("user-A", "conn-1")

    assert result == []


@pytest.mark.asyncio
async def test_list_channels_degrades_when_sync_state_fails():
    """Sync-state batch failure does not bring down the whole tool call."""
    conn = MagicMock()
    conn.id = "conn-1"
    conn.platform = "slack"
    conn.selected_channels = []

    mock_stores = MagicMock()
    mock_stores.platform.get_connection = AsyncMock(return_value=conn)
    mock_stores.mongodb.get_channel_sync_states_batch = AsyncMock(
        side_effect=RuntimeError("mongo down"),
    )

    discovery_result = [_ch("C1", "general")]

    with (
        patch(
            "beever_atlas.capabilities.connections.assert_connection_owned",
            return_value=None,
        ),
        patch(
            "beever_atlas.capabilities.connections.get_stores",
            return_value=mock_stores,
        ),
        patch(
            "beever_atlas.capabilities.connections.fetch_connection_channels_safe",
            AsyncMock(return_value=discovery_result),
        ),
    ):
        result = await list_channels("user-A", "conn-1")

    assert len(result) == 1
    # Still surfaces the channel, just with no sync-state enrichment.
    assert result[0]["sync_status"] == "never_synced"
    assert result[0]["name"] == "general"
