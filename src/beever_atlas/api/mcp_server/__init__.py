"""FastMCP server factory for the v2 /mcp mount.

This is the curated agent-facing surface introduced by openspec change
``atlas-mcp-server``. It is DISTINCT from ``src/beever_atlas/api/mcp.py`` —
the latter is the legacy unauthenticated mount (gated off by
``BEEVER_MCP_ENABLED=false`` via the Phase 0 hotfix) and will be removed once
all clients migrate to the v2 surface.

Phase 2 ships the factory skeleton with auth wiring in place; Phase 3 registers
the full tool catalog (15 tools), Phase 4 adds resources and prompts, Phase 5b
adds the long-running-job tools.

The only tool registered here in Phase 2 is the deprecation shim for
``search_channel_knowledge`` — callers upgrading from the legacy mount get a
structured ``tool_renamed`` error pointing at the replacement tools.
"""

from __future__ import annotations

import logging
from importlib.metadata import PackageNotFoundError, version

from fastmcp import FastMCP

logger = logging.getLogger(__name__)


def _atlas_version() -> str:
    """Best-effort package version for the MCP ``initialize`` server-info block."""
    try:
        return version("beever-atlas")
    except PackageNotFoundError:
        return "0.1.0"


def build_mcp() -> FastMCP:
    """Construct the v2 FastMCP instance used by the ``/mcp`` mount.

    Auth is enforced ONE level up by the ASGI
    :class:`~beever_atlas.infra.mcp_auth.MCPAuthMiddleware` wrapped around the
    :py:meth:`fastmcp.FastMCP.http_app` output — by the time a tool handler
    runs, ``scope["state"]["mcp_principal_id"]`` is populated with the caller's
    ``mcp:<hash>`` principal id. Tool handlers MUST read the principal from
    ``Context`` (or the request state) and call ``assert_channel_access`` /
    ``assert_connection_owned`` as the first line of their body.
    """
    mcp = FastMCP(
        name="beever-atlas",
        instructions=(
            "Beever Atlas MCP surface. Curated tools, resources, and prompts "
            "for external AI agents (Claude Code, Cursor, IDE assistants) to "
            "discover, query, and operate Atlas knowledge. Every tool that "
            "takes a channel_id or connection_id applies a principal-scoped "
            "ACL; unauthorized calls return structured error payloads with "
            "codes from the mcp-auth error catalog (channel_access_denied, "
            "connection_access_denied, job_not_found, rate_limited, etc.)."
        ),
        version=_atlas_version(),
    )

    _register_deprecation_shim(mcp)

    logger.info(
        "event=mcp_build name=beever-atlas version=%s tools_registered=%d",
        _atlas_version(),
        1,  # just the deprecation shim for Phase 2
    )
    return mcp


def _register_deprecation_shim(mcp: FastMCP) -> None:
    """Register the tool-renamed shim for the legacy ``search_channel_knowledge``.

    External integrations that already point at the old unauthenticated mount
    will receive a structured error pointing at the v2 replacements instead of
    a silent 404. This shim stays across all phases — one of the 3 reserved
    slots beyond the 15-tool v1 catalog (per design D5 revision).
    """

    @mcp.tool(
        name="search_channel_knowledge",
        description=(
            "DEPRECATED. The unauthenticated /mcp tool 'search_channel_knowledge' "
            "has been retired. Use 'ask_channel' for natural-language questions "
            "with citations, or 'search_channel_facts' for targeted BM25+vector "
            "fact search. This tool returns a structured tool_renamed error."
        ),
    )
    async def search_channel_knowledge_deprecated(
        channel_id: str = "",  # kept for signature compat with legacy callers
        query: str = "",
    ) -> dict:
        return {
            "error": "tool_renamed",
            "detail": (
                "search_channel_knowledge was replaced by ask_channel "
                "(streamed, cited answers) and search_channel_facts "
                "(structured fact search)."
            ),
            "replacement": ["ask_channel", "search_channel_facts"],
        }


__all__ = ["build_mcp"]
