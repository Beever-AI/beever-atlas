"""Session tools: start_new_session."""

from __future__ import annotations

import logging
import uuid as _uuid

from fastmcp import Context, FastMCP

from beever_atlas.api.mcp_server._helpers import _get_principal_id

logger = logging.getLogger(__name__)


def register_session_tools(mcp: FastMCP) -> None:

    @mcp.tool(name="start_new_session")
    async def start_new_session(ctx: Context) -> dict:
        """Mint a fresh conversation session id that starts a new ``ask_channel`` thread.

        Call this to begin a clean conversation that carries no memory of prior
        ``ask_channel`` turns — e.g. when the user switches to an unrelated topic
        or explicitly asks to "start over" / "forget previous context". Pass the
        returned id as the ``session_id`` argument on subsequent ``ask_channel``
        calls so they share one continuous thread; reuse the same id for
        follow-ups, and mint a new one only when you want a clean break.

        When NOT to use: do not call this before every question. ``ask_channel``
        auto-creates a session when ``session_id`` is omitted, so a new session
        id is only needed to intentionally drop earlier conversation context.

        Prerequisites: none. Requires an authenticated MCP principal (the caller's
        connection token); no ``channel_id`` or other input is needed.

        Returns: a dict ``{"session_id": "mcp:<principal>:<short>"}`` where the
        value is a fresh opaque conversation handle scoped to the caller, e.g.
        ``{"session_id": "mcp:conn_abc123:9f3c1a2b"}``. On a missing or invalid
        principal it returns ``{"error": "authentication_missing"}`` instead.

        Side effects: none — this allocates a new conversation boundary marker and
        does not delete, persist, or mutate any stored data. Latency: instant
        (no network or LLM call); safe to call synchronously inline.
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        short_id = str(_uuid.uuid4())[:8]
        session_id = f"mcp:{principal_id}:{short_id}"
        return {"session_id": session_id}
