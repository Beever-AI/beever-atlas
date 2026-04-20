"""Shared channel-discovery logic used by both the dashboard `/api/channels`
endpoint and the MCP/orchestration `list_channels` capability.

Previously this lived inside ``src/beever_atlas/api/channels.py`` as private
helpers (``_make_bridge_adapter``, ``_fetch_connection_channels``,
``_fetch_file_connection_channels``). The `list_channels` capability used
to return only ``conn.selected_channels``, which hid all non-file
connections whose sync pick-list was empty — even though the dashboard
sidebar showed them. Extracting the dashboard's discovery logic here
and having both callers use it is the single source of truth.

Behavioural notes:

- For ``platform == "file"`` connections, ``selected_channels`` IS the
  canonical list (file uploads don't have a bridge to query). Names are
  pulled from the activity log. ``is_member_only`` has no effect here
  since file channels are always "connected" in the synthesised sense.
- For all other platforms, the bridge adapter's ``list_channels()`` is
  called to get every available channel; ``selected_channels`` is then
  applied as a filter ONLY when it is non-empty (it is a sync pick-list,
  not an access ACL — see commit ba615c1).
- ``is_member_only`` (default ``False``): when ``True`` AND
  ``selected_channels`` is empty, only channels where the bot is an
  actual member (``ChannelInfo.is_member is True``) are returned. This
  matches what the dashboard's Channels page considers "CONNECTED" and is
  what the MCP/orchestration path wants so the QA agent can only surface
  channels it can actually read messages from. When ``selected_channels``
  is non-empty the user's explicit pick-list wins and the membership
  filter is skipped (the user opted in explicitly). The dashboard route
  keeps passing ``is_member_only=False`` because its UI needs both
  connected and available channels.
- A short-lived in-memory cache per ``connection_id`` prevents repeated
  bridge calls on rapid polling. The cache stores the raw bridge list so
  both filtered and unfiltered callers benefit; filters are applied after
  the cache read.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

from beever_atlas.adapters import ChannelInfo, get_adapter
from beever_atlas.adapters.bridge import ChatBridgeAdapter
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

_PER_CONNECTION_TIMEOUT = 10.0  # seconds — prevent one slow platform from blocking others
_CHANNEL_CACHE_TTL = 60.0  # seconds

# Process-local cache keyed by connection_id → (fetched_at_monotonic, channels)
_channel_cache: dict[str, tuple[float, list[ChannelInfo]]] = {}


def make_bridge_adapter(connection_id: str):
    """Construct a per-connection adapter, honouring ``ADAPTER_MOCK=true``.

    Mock mode routes to the MockAdapter singleton uniformly, so integration
    tests can drive the discovery code without a real bridge.
    """
    if os.environ.get("ADAPTER_MOCK", "").lower() in ("true", "1", "yes"):
        return get_adapter()
    return ChatBridgeAdapter(connection_id=connection_id)


async def fetch_file_connection_channels(
    conn_id: str,
    selected: list[str],
) -> list[ChannelInfo]:
    """Synthesise ChannelInfo for file-import channels from MongoDB.

    File connections don't live on the bridge — their channels are the list
    of ``selected_channels`` the connection tracks, with names pulled from
    the activity log.
    """
    if not selected:
        return []
    stores = get_stores()
    names = await asyncio.gather(
        *[stores.mongodb.get_channel_display_name(cid) for cid in selected]
    )
    return [
        ChannelInfo(
            channel_id=cid,
            name=name or cid,
            platform="file",
            is_member=True,
            connection_id=conn_id,
        )
        for cid, name in zip(selected, names)
    ]


async def fetch_connection_channels(
    conn_id: str,
    selected: list[str],
    platform: str = "",
    is_member_only: bool = False,
) -> list[ChannelInfo]:
    """Fetch the channels visible on one connection.

    Returns the full list (optionally filtered by ``selected`` when
    non-empty, or by bot membership when ``is_member_only=True`` and
    ``selected`` is empty). File connections synthesise from MongoDB;
    every other platform calls the bridge adapter.

    Each call creates a short-lived adapter that is closed after use to
    avoid leaking httpx connections. A per-connection timeout prevents
    one slow platform (e.g. Discord rate limits) from blocking callers.

    Filter precedence (non-file platforms):
      1. If ``selected`` is non-empty: filter to the pick-list, ignore
         ``is_member_only``. The user's explicit opt-in wins.
      2. Else if ``is_member_only`` is True: keep only channels where
         ``ChannelInfo.is_member`` is True.
      3. Else: return the full list from the bridge (dashboard default).
    """
    if platform == "file":
        return await fetch_file_connection_channels(conn_id, selected)

    cached = _channel_cache.get(conn_id)
    if cached:
        cached_at, cached_channels = cached
        if time.monotonic() - cached_at < _CHANNEL_CACHE_TTL:
            return _apply_filters(cached_channels, selected, is_member_only)

    adapter = make_bridge_adapter(conn_id)
    try:
        channels = await asyncio.wait_for(
            adapter.list_channels(),
            timeout=_PER_CONNECTION_TIMEOUT,
        )
        _channel_cache[conn_id] = (time.monotonic(), channels)
        return _apply_filters(channels, selected, is_member_only)
    finally:
        await adapter.close()


def _apply_filters(
    channels: list[ChannelInfo],
    selected: list[str],
    is_member_only: bool,
) -> list[ChannelInfo]:
    """Apply selected-pick-list or is_member_only filter to a channel list.

    See :func:`fetch_connection_channels` for precedence rules.
    """
    if selected:
        selected_set = set(selected)
        return [ch for ch in channels if ch.channel_id in selected_set]
    if is_member_only:
        return [ch for ch in channels if ch.is_member]
    return channels


async def fetch_connection_channels_safe(
    conn_id: str,
    selected: list[str],
    platform: str = "",
    is_member_only: bool = False,
) -> list[ChannelInfo]:
    """Same as :func:`fetch_connection_channels` but swallows bridge errors
    and returns an empty list on failure.

    Intended for the MCP/orchestration path where a single broken
    connection must not fail the whole tool call.
    """
    try:
        return await fetch_connection_channels(
            conn_id,
            selected,
            platform,
            is_member_only=is_member_only,
        )
    except Exception:
        logger.warning(
            "channel_discovery: fetch failed for connection=%s platform=%s",
            conn_id,
            platform,
            exc_info=True,
        )
        return []


__all__ = [
    "make_bridge_adapter",
    "fetch_file_connection_channels",
    "fetch_connection_channels",
    "fetch_connection_channels_safe",
]
