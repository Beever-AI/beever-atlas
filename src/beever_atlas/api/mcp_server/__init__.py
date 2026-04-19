"""FastMCP server factory for the /mcp mount.

This is the sole agent-facing MCP surface, introduced by openspec change
``atlas-mcp-server``. The legacy unauthenticated ``/mcp`` mount has been
retired; all clients connect through this curated, auth-gated surface.

Phase 2 shipped the factory skeleton with auth wiring; Phase 3 registers the
full tool catalog:

    Discovery      (3): whoami, list_connections, list_channels
    Retrieval      (5): ask_channel, search_channel_facts, get_wiki_page,
                        get_recent_activity, search_media_references
    Graph          (3): find_experts, search_relationships, trace_decision_history
    Session        (1): start_new_session
    Shim           (1): search_channel_knowledge  <- deprecation shim

Phase 4 adds resources and prompts.
Phase 5b adds the long-running-job tools:

    Orchestration  (3): trigger_sync, refresh_wiki, get_job_status

Phase 7 adds principal-keyed rate limiting and per-tool audit logging.

Tool-group submodules (split to keep each file under ~300 lines):
    _helpers.py              shared helpers (_get_principal_id, _validate_id, …)
    _tools_discovery.py      whoami, list_connections, list_channels
    _tools_retrieval.py      ask_channel, search_channel_facts, get_wiki_page,
                             get_recent_activity, search_media_references
    _tools_graph.py          find_experts, search_relationships, trace_decision_history
    _tools_session.py        start_new_session
    _tools_orchestration.py  trigger_sync, refresh_wiki, get_job_status
    _resources.py            atlas:// URI resources
    _prompts.py              summarize_channel, investigate_decision, onboard_new_channel
"""

from __future__ import annotations

import logging
import time
import uuid as _uuid
from functools import wraps
from typing import Any

from fastmcp import Context, FastMCP

from beever_atlas.api.mcp_server._helpers import (
    _atlas_version,
    _get_principal_id,
)
from beever_atlas.api.mcp_server._prompts import register_prompts
from beever_atlas.api.mcp_server._resources import register_resources
from beever_atlas.api.mcp_server._tools_discovery import register_discovery_tools
from beever_atlas.api.mcp_server._tools_graph import register_graph_tools
from beever_atlas.api.mcp_server._tools_orchestration import register_orchestration_tools
from beever_atlas.api.mcp_server._tools_retrieval import register_retrieval_tools
from beever_atlas.api.mcp_server._tools_session import register_session_tools

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Phase 7: Rate-limit helper
# ---------------------------------------------------------------------------

# Tools that skip rate limiting (deprecation shims return structured errors
# without touching any backend, so they are exempt).
_RATE_LIMIT_EXEMPT: frozenset[str] = frozenset({"search_channel_knowledge"})


async def _check_rate_limit(principal_id: str, tool_name: str) -> dict | None:
    """Check principal-keyed rate limit for *tool_name*.

    Returns a structured ``rate_limited`` error dict if the limit is exceeded,
    or ``None`` if the call is allowed.
    """
    if tool_name in _RATE_LIMIT_EXEMPT:
        return None

    from beever_atlas.infra import mcp_rate_limit

    allowed, retry = await mcp_rate_limit.check_and_record(principal_id, tool_name)
    if not allowed:
        return {"error": "rate_limited", "retry_after_seconds": retry}
    return None


# ---------------------------------------------------------------------------
# Phase 7: Audit log wrapper
# ---------------------------------------------------------------------------

def _get_request_id() -> str:
    """Extract the MCP request id from the current ASGI scope, or generate one."""
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        state = request.scope.get("state") or {}
        return state.get("mcp_request_id") or str(_uuid.uuid4())
    except Exception:
        return str(_uuid.uuid4())


def _extract_target(kwargs: dict[str, Any]) -> str | None:
    """Best-effort extraction of the resource id from tool kwargs."""
    for key in ("channel_id", "connection_id", "job_id"):
        val = kwargs.get(key)
        if val:
            return str(val)
    return None


def _audit_tool(tool_name: str):
    """Decorator factory: wraps a tool function with rate limiting and audit logging.

    Usage inside a ``@mcp.tool`` registration::

        @mcp.tool(name="my_tool")
        @_audit_tool("my_tool")
        async def my_tool(ctx: Context, ...) -> dict:
            ...

    The decorator:
    1. Extracts the principal id.
    2. Checks the rate limit — returns the structured error immediately if exceeded.
    3. Records start time.
    4. Calls the wrapped function.
    5. Emits one ``mcp_tool_call`` structured log line with request_id, principal,
       tool, target, outcome, and duration_ms.
    6. On unhandled exception: emits with ``outcome="exception"`` and re-raises.
    """
    def decorator(fn):
        @wraps(fn)
        async def wrapper(*args, **kwargs):
            # Extract ctx — it's always a keyword arg named 'ctx' or the first
            # positional arg after self when there is no explicit self.
            ctx: Context | None = kwargs.get("ctx")
            if ctx is None:
                for arg in args:
                    if isinstance(arg, Context):
                        ctx = arg
                        break

            principal_id = _get_principal_id(ctx) if ctx is not None else None
            request_id = _get_request_id()
            target = _extract_target(kwargs)

            # --- Rate limit check ---
            if principal_id:
                rate_err = await _check_rate_limit(principal_id, tool_name)
                if rate_err is not None:
                    _emit_audit(
                        request_id=request_id,
                        principal=principal_id or "anonymous",
                        tool=tool_name,
                        target=target,
                        outcome="rate_limited",
                        duration_ms=0.0,
                    )
                    return rate_err

            # --- Execute ---
            start = time.monotonic()
            outcome = "ok"
            try:
                result = await fn(*args, **kwargs)
                # Derive outcome from structured error keys in the result.
                if isinstance(result, dict) and "error" in result:
                    outcome = result["error"]
                return result
            except Exception:
                outcome = "exception"
                _emit_audit(
                    request_id=request_id,
                    principal=principal_id or "anonymous",
                    tool=tool_name,
                    target=target,
                    outcome=outcome,
                    duration_ms=(time.monotonic() - start) * 1000,
                )
                raise
            finally:
                if outcome != "exception":
                    duration_ms = (time.monotonic() - start) * 1000
                    _emit_audit(
                        request_id=request_id,
                        principal=principal_id or "anonymous",
                        tool=tool_name,
                        target=target,
                        outcome=outcome,
                        duration_ms=duration_ms,
                    )

        return wrapper
    return decorator


def _emit_audit(
    *,
    request_id: str,
    principal: str,
    tool: str,
    target: str | None,
    outcome: str,
    duration_ms: float,
) -> None:
    """Emit one structured audit log line and one metrics record."""
    data: dict[str, Any] = {
        "event": "mcp_tool_call",
        "request_id": request_id,
        "principal": principal,
        "tool": tool,
        "outcome": outcome,
        "duration_ms": round(duration_ms, 2),
    }
    if target is not None:
        data["target"] = target

    logger.info(
        "event=mcp_tool_call request_id=%s principal=%s tool=%s outcome=%s "
        "duration_ms=%.2f target=%s",
        request_id,
        principal,
        tool,
        outcome,
        duration_ms,
        target,
        extra={"cat": "mcp", "data": data},
    )

    # Also emit per-tool metric.
    try:
        from beever_atlas.infra.mcp_metrics import record_tool_call

        record_tool_call(
            tool_name=tool,
            principal_hash=principal,
            outcome=outcome,
            duration_ms=duration_ms,
            target=target,
        )
    except Exception:
        logger.debug("mcp metrics emit failed for tool=%s", tool, exc_info=True)


# ---------------------------------------------------------------------------
# Deprecation shim (Phase 2, retained permanently)
# ---------------------------------------------------------------------------

def _register_deprecation_shim(mcp: FastMCP) -> None:
    """Register the tool-renamed shim for the legacy ``search_channel_knowledge``.

    External integrations still pointing at the retired tool name will receive
    a structured error pointing at the replacements instead of a silent 404.
    This shim is exempt from rate limiting.
    """

    @mcp.tool(
        name="search_channel_knowledge",
        description=(
            "DEPRECATED. 'search_channel_knowledge' has been retired. Use "
            "'ask_channel' for natural-language questions with citations, or "
            "'search_channel_facts' for targeted BM25+vector fact search. "
            "This tool returns a structured tool_renamed error."
        ),
    )
    async def search_channel_knowledge_deprecated(
        channel_id: str = "",
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


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def build_mcp() -> FastMCP:
    """Construct the v2 FastMCP instance used by the ``/mcp`` mount.

    Auth is enforced ONE level up by the ASGI
    :class:`~beever_atlas.infra.mcp_auth.MCPAuthMiddleware` wrapped around the
    :py:meth:`fastmcp.FastMCP.http_app` output — by the time a tool handler
    runs, ``scope["state"]["mcp_principal_id"]`` is populated with the caller's
    ``mcp:<hash>`` principal id.

    Phase 7: every tool invocation is wrapped by :func:`_audit_tool` which
    enforces the principal-keyed rate limit, records start time, and emits one
    structured audit log + metric on completion.
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
    register_discovery_tools(mcp)
    register_retrieval_tools(mcp)
    register_graph_tools(mcp)
    register_session_tools(mcp)
    register_orchestration_tools(mcp)
    register_resources(mcp)
    register_prompts(mcp)

    # Phase 7: patch every registered tool with the audit+rate-limit wrapper.
    _apply_audit_wrappers(mcp)

    tool_count = sum(
        1 for k in mcp._local_provider._components if k.startswith("tool:")
    )
    logger.info(
        "event=mcp_build name=beever-atlas version=%s tools_registered=%d",
        _atlas_version(),
        tool_count,
    )
    return mcp


def _apply_audit_wrappers(mcp: FastMCP) -> None:
    """Wrap every registered tool function with the audit+rate-limit decorator.

    FastMCP stores tools in ``mcp._local_provider._components`` as objects with
    a ``.fn`` attribute (the raw async function). We replace ``.fn`` with the
    wrapped version after all tools are registered so decorating at registration
    time is not needed in each submodule.
    """
    for key, component in mcp._local_provider._components.items():
        if not key.startswith("tool:"):
            continue
        # Extract the tool name from the registry key ("tool:<name>@<version>"
        # or "tool:<name>").
        raw_name = key[len("tool:"):]
        tool_name = raw_name.split("@")[0]

        if not hasattr(component, "fn"):
            continue

        component.fn = _audit_tool(tool_name)(component.fn)


__all__ = ["build_mcp"]
