"""Unit tests for ``beever_atlas.services.channel_discovery``.

These tests cover the ``is_member_only`` parameter on
``fetch_connection_channels`` (and its ``_safe`` wrapper):

- Default (``is_member_only=False``) returns every channel the bridge
  reports, matching the dashboard's need for CONNECTED + AVAILABLE.
- ``is_member_only=True`` with an empty ``selected`` list filters out
  channels where the bot is not a member — what the MCP/orchestration
  path wants so the QA agent only sees readable channels.
- ``is_member_only=True`` is IGNORED when ``selected`` is non-empty (the
  user's explicit pick-list wins, even for non-member channels).
- File-platform connections ignore ``is_member_only`` and resolve
  entirely from ``selected``.

The bridge adapter is patched at
``beever_atlas.services.channel_discovery.make_bridge_adapter`` so no
real HTTP client is instantiated.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.adapters import ChannelInfo
from beever_atlas.services import channel_discovery
from beever_atlas.services.channel_discovery import (
    fetch_connection_channels,
    fetch_connection_channels_safe,
)


def _ch(
    channel_id: str,
    name: str,
    *,
    is_member: bool = True,
    platform: str = "slack",
    connection_id: str = "conn-1",
) -> ChannelInfo:
    return ChannelInfo(
        channel_id=channel_id,
        name=name,
        platform=platform,
        is_member=is_member,
        connection_id=connection_id,
    )


@pytest.fixture(autouse=True)
def _clear_cache():
    """Isolate the module-level channel cache across tests."""
    channel_discovery._channel_cache.clear()
    yield
    channel_discovery._channel_cache.clear()


def _patched_adapter(channels: list[ChannelInfo]):
    """Build a MagicMock adapter whose list_channels returns *channels*."""
    adapter = MagicMock()
    adapter.list_channels = AsyncMock(return_value=channels)
    adapter.close = AsyncMock(return_value=None)
    return adapter


@pytest.mark.asyncio
async def test_is_member_only_false_returns_all_channels():
    """Default behavior: no filter is applied — caller gets the full list."""
    channels = [
        _ch("C1", "general", is_member=True),
        _ch("C2", "random", is_member=False),
        _ch("C3", "off-topic", is_member=False),
    ]
    adapter = _patched_adapter(channels)

    with patch(
        "beever_atlas.services.channel_discovery.make_bridge_adapter",
        return_value=adapter,
    ):
        result = await fetch_connection_channels(
            "conn-1",
            [],
            platform="slack",
        )

    assert {ch.channel_id for ch in result} == {"C1", "C2", "C3"}
    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_is_member_only_true_filters_non_member_channels_when_selected_empty():
    """is_member_only=True + empty selected → non-member channels dropped."""
    channels = [
        _ch("C1", "general", is_member=True),
        _ch("C2", "random", is_member=False),
        _ch("C3", "announcements", is_member=True),
        _ch("C4", "private", is_member=False),
    ]
    adapter = _patched_adapter(channels)

    with patch(
        "beever_atlas.services.channel_discovery.make_bridge_adapter",
        return_value=adapter,
    ):
        result = await fetch_connection_channels(
            "conn-1",
            [],
            platform="slack",
            is_member_only=True,
        )

    assert {ch.channel_id for ch in result} == {"C1", "C3"}
    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_is_member_only_true_ignored_when_selected_non_empty():
    """Explicit selected pick-list wins over is_member_only.

    The user opted in explicitly; membership must not override that.
    """
    channels = [
        _ch("C1", "general", is_member=True),
        _ch("C2", "random", is_member=False),
        _ch("C3", "announcements", is_member=False),
        _ch("C4", "unrelated", is_member=True),
    ]
    adapter = _patched_adapter(channels)

    with patch(
        "beever_atlas.services.channel_discovery.make_bridge_adapter",
        return_value=adapter,
    ):
        result = await fetch_connection_channels(
            "conn-1",
            ["C1", "C2", "C3"],
            platform="slack",
            is_member_only=True,
        )

    # All three selected returned — including the non-member ones.
    assert {ch.channel_id for ch in result} == {"C1", "C2", "C3"}
    adapter.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_file_platform_ignores_is_member_only():
    """File connections synthesise from selected; is_member_only is a no-op.

    File uploads have no bridge to query, so the selected pick-list is
    the canonical channel list regardless of membership semantics.
    """
    stores = MagicMock()
    stores.mongodb.get_channel_display_name = AsyncMock(
        side_effect=lambda cid: f"{cid}.pdf",
    )

    with (
        patch(
            "beever_atlas.services.channel_discovery.get_stores",
            return_value=stores,
        ),
        patch(
            "beever_atlas.services.channel_discovery.make_bridge_adapter",
        ) as adapter_mock,
    ):
        result = await fetch_connection_channels(
            "conn-file",
            ["file-abc", "file-xyz"],
            platform="file",
            is_member_only=True,
        )

    # Bridge adapter must NOT be constructed for file connections.
    adapter_mock.assert_not_called()
    assert {ch.channel_id for ch in result} == {"file-abc", "file-xyz"}
    assert {ch.platform for ch in result} == {"file"}
    assert {ch.name for ch in result} == {"file-abc.pdf", "file-xyz.pdf"}


@pytest.mark.asyncio
async def test_safe_wrapper_forwards_is_member_only():
    """fetch_connection_channels_safe passes is_member_only through."""
    channels = [
        _ch("C1", "general", is_member=True),
        _ch("C2", "random", is_member=False),
    ]
    adapter = _patched_adapter(channels)

    with patch(
        "beever_atlas.services.channel_discovery.make_bridge_adapter",
        return_value=adapter,
    ):
        result = await fetch_connection_channels_safe(
            "conn-1",
            [],
            platform="slack",
            is_member_only=True,
        )

    assert {ch.channel_id for ch in result} == {"C1"}
