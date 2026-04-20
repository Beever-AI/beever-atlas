"""Discovery tools: whoami, list_connections, list_channels (Phase 3, tasks 3.1–3.3)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastmcp import Context, FastMCP

from beever_atlas.api.mcp_server._helpers import (
    _atlas_version,
    _get_principal_id,
    _validate_id,
)

logger = logging.getLogger(__name__)


def register_discovery_tools(mcp: FastMCP) -> None:

    @mcp.tool(name="whoami")
    async def whoami(ctx: Context) -> dict:
        """Return the authenticated principal's identity and accessible connections.

        Use this tool at the start of a session to discover your principal id and
        the list of connection ids you can access. The returned ``connections`` list
        contains only ids — call ``list_connections`` for full connection metadata.
        The ``server_version`` field reflects the deployed Atlas version.

        When to use: first call in a session, before any other tool, to verify
        authentication succeeded and to obtain connection ids for ``list_channels``.
        Do NOT call repeatedly — the response is stable within a session.

        Returns: ``{principal_id, connections, server_version}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            logger.warning("event=mcp_tool_missing_principal tool=whoami")
            return {"error": "authentication_missing"}

        try:
            from beever_atlas.capabilities import connections as conn_cap

            conns = await conn_cap.list_connections(principal_id)
            connection_ids = [c["connection_id"] for c in conns]
        except Exception:
            logger.exception("whoami: list_connections failed for principal=%s", principal_id)
            connection_ids = []

        return {
            "principal_id": principal_id,
            "connections": connection_ids,
            "server_version": _atlas_version(),
        }

    @mcp.tool(name="list_connections")
    async def list_connections(ctx: Context) -> dict:
        """Return all platform connections (Slack workspaces, Discord servers, etc.) accessible to this principal.

        Each connection entry contains: ``connection_id``, ``platform``,
        ``display_name``, ``status``, ``last_synced_at``,
        ``selected_channel_count``, and ``source``.

        When to use: to enumerate which workspaces/servers this principal can
        access, then drill into a specific connection with ``list_channels``.
        Results are filtered by ownership — you only see your own connections.

        **IMPORTANT — field semantics (don't misread these):**

        - ``selected_channel_count`` is the size of the user's **sync pick-list**
          for this connection. It is NOT the number of channels available on
          the platform. ``selected_channel_count: 0`` means no channels are
          explicitly opted into sync — it does NOT mean the connection has no
          channels. A Slack workspace with 0 selected channels can still have
          dozens of channels the bot can read.
        - ``last_synced_at`` is scoped to the same pick-list. When the pick-list
          is empty, this field is ``null`` even if channels on the connection
          were synced through another path. Do not use it to infer "this
          connection has never been used."

        **To discover actual channels, always call ``list_channels(connection_id)``**
        — that reads the live platform catalog (scoped to channels the bot can
        read) and is the ground truth. Do NOT infer channel availability from
        ``selected_channel_count``.

        Returns: ``{connections: [<connection dict>, ...]}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            logger.warning("event=mcp_tool_missing_principal tool=list_connections")
            return {"error": "authentication_missing"}

        try:
            from beever_atlas.capabilities import connections as conn_cap

            conns = await conn_cap.list_connections(principal_id)
            return {"connections": conns}
        except Exception:
            logger.exception("list_connections: capability failed for principal=%s", principal_id)
            return {"connections": []}

    @mcp.tool(name="list_channels")
    async def list_channels(
        connection_id: Annotated[
            str, "The connection id to list channels for (from list_connections)"
        ],
        ctx: Context,
    ) -> dict:
        """Return the channels the bot can actually read on a connection.

        This is the **ground truth** for what channels exist on this connection —
        always prefer it over ``list_connections.selected_channel_count``.

        Each channel entry contains: ``channel_id``, ``name``, ``platform``,
        ``last_sync_ts``, ``sync_status``, and ``message_count_estimate``.

        Scoping: the returned set matches the dashboard's "CONNECTED" view.
        When the user has picked specific channels for sync
        (``selected_channels`` non-empty on the connection), those are returned.
        Otherwise every channel where the bot is a member (``is_member=True``)
        is returned — because those are exactly the channels the bot can read
        messages from. File-import connections return every file that has been
        uploaded.

        When to use: after ``list_connections``, to enumerate real channels
        before calling any retrieval tool. Always call this per connection you
        care about. ``sync_status="never_synced"`` on a channel is normal and
        does NOT mean the channel is inaccessible — it just hasn't been indexed
        yet. ``trigger_sync(channel_id)`` can be used to ingest it.

        Raises a structured ``connection_access_denied`` error if the principal
        does not own the requested connection — connection existence is not leaked.

        Returns: ``{channels: [...]}`` or ``{error: "connection_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            logger.warning("event=mcp_tool_missing_principal tool=list_channels")
            return {"error": "authentication_missing"}

        err = _validate_id(connection_id, "connection_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import connections as conn_cap
            from beever_atlas.capabilities.errors import ConnectionAccessDenied

            channels = await conn_cap.list_channels(principal_id, connection_id)
            return {"channels": channels}
        except ConnectionAccessDenied:
            return {
                "error": "connection_access_denied",
                "connection_id": connection_id,
            }
        except Exception:
            logger.exception(
                "list_channels: capability failed principal=%s connection_id=%s",
                principal_id,
                connection_id,
            )
            return {"channels": []}
