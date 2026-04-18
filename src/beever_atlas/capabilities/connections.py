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


async def list_connections(principal_id: str) -> list[dict]:
    """Return the list of platform connections visible to *principal_id*.

    Filtering rules:

    1. If ``connection.owner_principal_id == principal_id`` → always included.
    2. Single-tenant fallback (``BEEVER_SINGLE_TENANT=true`` AND
       ``owner_principal_id in {None, "legacy:shared"}``) → included.
    3. Everything else → excluded.

    The returned dicts contain:
    ``connection_id, platform, display_name, status, last_synced_at,
    selected_channel_count, source``.
    """
    stores = get_stores()
    connections = await stores.platform.list_connections()
    single_tenant = _is_single_tenant()

    results: list[dict] = []
    for conn in connections:
        owner = getattr(conn, "owner_principal_id", None)
        owned = owner == principal_id
        legacy = owner in (None, _LEGACY_SHARED_OWNER)
        if owned or (single_tenant and legacy):
            source = owned if owned else "inherited"
            results.append({
                "connection_id": conn.id,
                "platform": conn.platform,
                "display_name": conn.display_name,
                "status": conn.status,
                "last_synced_at": None,  # not yet in PlatformConnection model
                "selected_channel_count": len(conn.selected_channels or []),
                "source": conn.source,
            })
    return results


async def list_channels(principal_id: str, connection_id: str) -> list[dict]:
    """Return the channels on *connection_id* visible to *principal_id*.

    Raises :class:`~capabilities.errors.ConnectionAccessDenied` when the
    principal does not own the connection (mirrors ``assert_connection_owned``
    semantics: existence is not leaked).

    The returned dicts contain:
    ``channel_id, name, platform, last_sync_ts, sync_status,
    message_count_estimate``.

    Fields not yet stored on the connection model are ``None``.
    """
    # Will raise ConnectionAccessDenied if principal doesn't own the connection.
    await assert_connection_owned(principal_id, connection_id)

    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        # assert_connection_owned already raised; this path is unreachable in
        # practice but keeps the return type honest.
        raise ConnectionAccessDenied(connection_id)

    results: list[dict] = []
    for channel_id in conn.selected_channels or []:
        results.append({
            "channel_id": channel_id,
            "name": channel_id,  # display name not in model yet
            "platform": conn.platform,
            "last_sync_ts": None,
            "sync_status": None,
            "message_count_estimate": None,
        })
    return results


__all__ = [
    "list_connections",
    "list_channels",
]
