"""Platform connection capabilities: list connections and list channels.

Framework-neutral implementations for openspec change ``atlas-mcp-server``
Phase 1 (tasks 1.2, 1.3). The ADK tool layer and the forthcoming MCP layer
both call these directly.

``list_connections`` filters by ``owner_principal_id`` and honours the
single-tenant sentinel so pre-migration rows remain accessible to the
operator. ``list_channels`` enforces connection ownership via
:func:`~beever_atlas.infra.channel_access.assert_connection_owned`.
"""

from __future__ import annotations

import logging
import os

from beever_atlas.capabilities.errors import ConnectionAccessDenied
from beever_atlas.infra.channel_access import assert_connection_owned
from beever_atlas.services.channel_discovery import fetch_connection_channels_safe
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

_LEGACY_SHARED_OWNER = "legacy:shared"


def _is_single_tenant() -> bool:
    """Return True when BEEVER_SINGLE_TENANT is set (default True)."""
    try:
        from beever_atlas.infra.config import get_settings

        return bool(getattr(get_settings(), "beever_single_tenant", True))
    except Exception:
        return os.environ.get("BEEVER_SINGLE_TENANT", "true").lower() != "false"


async def _batch_sync_states(stores, channel_ids: list[str]) -> dict:
    """Batch-fetch ChannelSyncState keyed by channel_id; empty dict on failure."""
    if not channel_ids:
        return {}
    try:
        return await stores.mongodb.get_channel_sync_states_batch(channel_ids)
    except Exception:
        logger.debug(
            "capabilities.connections: sync-state batch fetch failed",
            exc_info=True,
        )
        return {}


async def list_connections(principal_id: str) -> list[dict]:
    """Return the list of platform connections visible to *principal_id*.

    Filtering rules:

    1. If ``connection.owner_principal_id == principal_id`` → always included.
    2. Single-tenant fallback (``BEEVER_SINGLE_TENANT=true`` AND
       ``owner_principal_id in {None, "legacy:shared"}``) → included.
    3. Single-tenant fallback for MCP principals: when
       ``BEEVER_SINGLE_TENANT=true`` AND the caller is an MCP principal
       (``principal_id`` starts with ``"mcp:"``), inherit ALL rows —
       including rows owned by a user principal. In single-tenant mode
       the MCP api-key represents the same operator as the dashboard
       user, so dashboard-created connections (stamped with the user's
       principal id) must remain reachable via MCP. This is scoped to
       MCP only: user principals still stay on the ``{None, "legacy:shared"}``
       fallback so they cannot see another user's rows in a single-tenant
       deployment with multiple API keys.
    4. Everything else → excluded.

    The returned dicts contain:
    ``connection_id, platform, display_name, status, last_synced_at,
    selected_channel_count, source``.

    ``last_synced_at`` is the most recent ``last_sync_ts`` across the
    connection's selected channels, or ``None`` when no channel has been
    synced or when the connection is a file connection (files aren't synced).
    """
    stores = get_stores()
    connections = await stores.platform.list_connections()
    single_tenant = _is_single_tenant()

    # In single-tenant mode, an MCP principal represents the same operator
    # as the dashboard user and inherits every connection (including rows
    # stamped with a user principal id). See rule 3 in the docstring.
    mcp_single_tenant = single_tenant and principal_id.startswith("mcp:")

    visible = []
    all_selected_ids: set[str] = set()
    for conn in connections:
        owner = getattr(conn, "owner_principal_id", None)
        owned = owner == principal_id
        legacy = owner in (None, _LEGACY_SHARED_OWNER)
        if owned or (single_tenant and legacy) or mcp_single_tenant:
            visible.append(conn)
            if conn.platform != "file":
                for cid in conn.selected_channels or []:
                    all_selected_ids.add(cid)

    states_map = await _batch_sync_states(stores, list(all_selected_ids))

    results: list[dict] = []
    for conn in visible:
        selected = conn.selected_channels or []
        if conn.platform == "file" or not selected:
            last_synced_at = None
        else:
            timestamps = [states_map[cid].last_sync_ts for cid in selected if cid in states_map]
            last_synced_at = max(timestamps) if timestamps else None
        results.append(
            {
                "connection_id": conn.id,
                "platform": conn.platform,
                "display_name": conn.display_name,
                "status": conn.status,
                "last_synced_at": last_synced_at,
                "selected_channel_count": len(selected),
                "source": conn.source,
            }
        )
    return results


async def list_channels(principal_id: str, connection_id: str) -> list[dict]:
    """Return the channels on *connection_id* visible to *principal_id*.

    Raises :class:`~capabilities.errors.ConnectionAccessDenied` when the
    principal does not own the connection (mirrors ``assert_connection_owned``
    semantics: existence is not leaked).

    Channel discovery is delegated to
    :func:`beever_atlas.services.channel_discovery.fetch_connection_channels_safe`
    — the same path used by the dashboard's ``/api/channels`` endpoint,
    except this capability passes ``is_member_only=True`` so non-file
    platforms are filtered to channels the bot is actually a member of
    (i.e. channels it can read messages from). That matches what the
    dashboard Channels page considers "CONNECTED". The dashboard itself
    keeps ``is_member_only=False`` so it can still render the
    CONNECTED/AVAILABLE split. Specifically:

    - For file connections (``platform == "file"``), the list is
      ``selected_channels`` with filenames pulled from the activity log.
    - For every other platform, the bridge adapter is queried for the full
      channel list. When ``selected_channels`` is non-empty it is applied
      as a filter — the user's explicit pick-list wins. When
      ``selected_channels`` is empty, only channels where
      ``ChannelInfo.is_member`` is True are returned (so the agent only
      sees channels it can actually read). If the bridge errors out, an
      empty list is returned rather than failing the whole tool call.

    The returned dicts contain:
    ``channel_id, name, platform, last_sync_ts, sync_status,
    message_count_estimate``.

    File connections report ``sync_status = "n/a"`` since uploaded files
    are not synced; ``last_sync_ts`` / ``message_count_estimate`` stay
    ``None``.
    """
    # Will raise ConnectionAccessDenied if principal doesn't own the connection.
    await assert_connection_owned(principal_id, connection_id)

    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        # assert_connection_owned already raised; this path is unreachable in
        # practice but keeps the return type honest.
        raise ConnectionAccessDenied(connection_id)

    channels = await fetch_connection_channels_safe(
        conn.id,
        conn.selected_channels or [],
        conn.platform,
        is_member_only=True,
    )
    if not channels:
        return []

    is_file_conn = conn.platform == "file"
    channel_ids = [ch.channel_id for ch in channels]
    states_map = {} if is_file_conn else await _batch_sync_states(stores, channel_ids)

    results: list[dict] = []
    for ch in channels:
        state = states_map.get(ch.channel_id) if not is_file_conn else None
        if is_file_conn:
            sync_status: str | None = "n/a"
            last_sync_ts: str | None = None
            message_count: int | None = None
        else:
            sync_status = "synced" if state else "never_synced"
            last_sync_ts = getattr(state, "last_sync_ts", None) if state else None
            message_count = getattr(state, "total_synced_messages", None) if state else None
        results.append(
            {
                "channel_id": ch.channel_id,
                "name": ch.name or ch.channel_id,
                "platform": ch.platform or conn.platform,
                "last_sync_ts": last_sync_ts,
                "sync_status": sync_status,
                "message_count_estimate": message_count,
            }
        )
    return results


__all__ = [
    "list_connections",
    "list_channels",
]
