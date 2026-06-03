"""Discovery tools: whoami, list_connections, list_channels."""

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
        """Confirm who you are authenticated as and which connection ids you can reach.

        Call this FIRST in any session, before any other tool, to (1) verify your
        auth token resolved to a principal and (2) get the connection ids needed by
        ``list_channels``. Returns only connection IDS here; use ``list_connections``
        when you also need each connection's platform, status, and sync metadata.

        When to use: once at session start. Do NOT call repeatedly — the response is
        stable for the whole session.

        Latency: instant (single in-memory/DB lookup; never triggers a sync or job).

        Returns a dict:
        - ``principal_id`` (str): your authenticated identity, e.g. ``"user_42"``.
        - ``connections`` (list[str]): connection ids you may access, e.g.
          ``["conn_abc123", "conn_def456"]``. Empty list if you own no connections.
        - ``server_version`` (str): deployed Atlas version, e.g. ``"0.1.0"``.

        Error modes: returns ``{"error": "authentication_missing"}`` when the request
        carries no valid principal (token absent/invalid). No access-denied path —
        the response is always scoped to the caller.
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
        """List the platform connections (Slack workspaces, Discord servers, file
        imports, etc.) this principal owns, with each connection's metadata.

        Use this when you need a connection's platform, status, or sync metadata —
        not just its id. If you only need the connection ids, ``whoami`` is cheaper.
        After picking a connection here, call ``list_channels(connection_id)`` to see
        its actual channels. Results are ownership-filtered: you only see your own.

        Latency: instant (read-only; no sync triggered).

        Returns ``{"connections": [<entry>, ...]}`` (empty list if none). Each entry:
        - ``connection_id`` (str): e.g. ``"conn_abc123"`` — pass to ``list_channels``.
        - ``platform`` (str): e.g. ``"slack"``, ``"discord"``, ``"file"``.
        - ``display_name`` (str): human label, e.g. ``"Acme Workspace"``.
        - ``status`` (str): connection health, e.g. ``"connected"``.
        - ``last_synced_at`` (str|null): ISO timestamp of the most recent sync of a
          PICKED channel; ``null`` when the pick-list is empty (see caveat below).
        - ``selected_channel_count`` (int): size of the sync pick-list (see caveat).
        - ``source`` (str): how the connection was created, e.g. ``"oauth"``.

        Caveats (do NOT misread): ``selected_channel_count`` is the user's opted-in
        sync pick-list, NOT how many channels exist. A value of ``0`` does not mean
        the connection is empty — a Slack workspace with 0 picks can still have
        dozens of bot-readable channels. ``last_synced_at`` is scoped to the same
        pick-list, so an empty pick-list yields ``null`` even if channels were synced
        another way. For ground-truth channel availability, always call
        ``list_channels(connection_id)`` — never infer it from these counts.

        Error modes: returns ``{"error": "authentication_missing"}`` when no valid
        principal is attached. Never raises access-denied (output is self-scoped).
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
            str,
            "Id of the connection whose channels to list, obtained from "
            "list_connections or whoami. Format: alphanumeric/_/:/- up to 128 "
            'chars. Example: "conn_abc123".',
        ],
        ctx: Context,
    ) -> dict:
        """List the channels the bot can actually read on one connection — the
        ground-truth source for what channels exist and their indexing state.

        Call this after ``list_connections``/``whoami`` (you need a valid
        ``connection_id``), once per connection you care about, BEFORE any retrieval
        tool (``ask_channel``, ``search_channel_facts``, ``get_wiki_page``, ...) so
        you can pass a real ``channel_id``. Prefer this over
        ``list_connections.selected_channel_count`` — that count is a sync pick-list,
        not the channel inventory.

        Latency: instant for cached results; may take a few seconds when it queries
        the live platform bridge. Read-only — does not trigger a sync.

        Returns ``{"channels": [<entry>, ...]}`` (empty list if none/bridge error).
        Each entry:
        - ``channel_id`` (str): pass to retrieval/sync tools, e.g. ``"C0A955E29MX"``.
        - ``name`` (str): display name, e.g. ``"engineering"``.
        - ``platform`` (str): e.g. ``"slack"``, ``"discord"``, ``"file"``.
        - ``last_sync_ts`` (str|null): ISO timestamp of last index, ``null`` if never.
        - ``sync_status`` (str): ``"synced"``, ``"never_synced"``, or ``"n/a"`` (file
          connections). ``"never_synced"`` is normal and does NOT mean the channel is
          inaccessible — it just is not indexed yet; call ``trigger_sync(channel_id)``
          to ingest it before querying its content.
        - ``message_count_estimate`` (int|null): approx synced messages, ``null`` if
          not yet synced.

        Scoping: matches the dashboard "CONNECTED" view. If the user picked specific
        channels for sync, those are returned; otherwise every channel where the bot
        is a member (and thus can read) is returned. File connections return every
        uploaded file.

        Error modes: ``{"error": "connection_access_denied", "connection_id": ...}``
        if you do not own the connection (existence is not leaked);
        ``{"error": "invalid_parameter", "parameter": "connection_id"}`` for a
        malformed id; ``{"error": "authentication_missing"}`` if no principal.
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
