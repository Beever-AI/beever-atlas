"""Session tools: start_new_session (Phase 3, task 3.7)."""

from __future__ import annotations

import logging
import uuid as _uuid

from fastmcp import Context, FastMCP

from beever_atlas.api.mcp_server._helpers import _get_principal_id

logger = logging.getLogger(__name__)


def register_session_tools(mcp: FastMCP) -> None:

    @mcp.tool(name="start_new_session")
    async def start_new_session(ctx: Context) -> dict:
        """Reset the conversation session and obtain a new session id.

        Call this when you want to start a fresh conversation thread — for
        example, after switching topics or to avoid carrying over context from
        a previous ``ask_channel`` conversation. The returned ``session_id``
        can be passed as the ``session_id`` parameter to ``ask_channel``.

        Note: this is a Phase 3 stub. Actual ADK session reset is wired in
        Phase 6. The stub returns a new unique session id that ``ask_channel``
        will accept as a conversation boundary marker.

        When to use: explicitly, only when the user asks to "start over" or
        "forget previous context". Do NOT call before every question.

        Returns: ``{session_id: "mcp:<principal>:<short_id>"}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        short_id = str(_uuid.uuid4())[:8]
        session_id = f"mcp:{principal_id}:{short_id}"
        return {"session_id": session_id}
