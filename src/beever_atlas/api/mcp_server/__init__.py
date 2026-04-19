"""FastMCP server factory for the v2 /mcp mount.

This is the curated agent-facing surface introduced by openspec change
``atlas-mcp-server``. It is DISTINCT from ``src/beever_atlas/api/mcp.py`` —
the latter is the legacy unauthenticated mount (gated off by
``BEEVER_MCP_ENABLED=false`` via the Phase 0 hotfix) and will be removed once
all clients migrate to the v2 surface.

Phase 2 shipped the factory skeleton with auth wiring; Phase 3 registers the
full tool catalog:

    Discovery      (3): whoami, list_connections, list_channels
    Retrieval      (5): ask_channel, search_channel_facts, get_wiki_page,
                        get_recent_activity, search_media_references
    Graph          (3): find_experts, search_relationships, trace_decision_history
    Session        (1): start_new_session
    Shim           (1): search_channel_knowledge  ← deprecation shim

Phase 4 adds resources and prompts.
Phase 5b adds the long-running-job tools:

    Orchestration  (3): trigger_sync, refresh_wiki, get_job_status
"""

from __future__ import annotations

import logging
import re
import uuid as _uuid
from importlib.metadata import PackageNotFoundError, version
from typing import Annotated

from fastmcp import FastMCP
from fastmcp import Context

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_INPUT_REGEX = re.compile(r"^[A-Za-z0-9_:\-]{1,128}$")


def _atlas_version() -> str:
    """Best-effort package version for the MCP ``initialize`` server-info block."""
    try:
        return version("beever-atlas")
    except PackageNotFoundError:
        return "0.1.0"


def _get_principal_id(ctx: Context) -> str | None:
    """Extract ``mcp_principal_id`` from the ASGI scope injected by MCPAuthMiddleware.

    The middleware sets ``scope["state"]["mcp_principal_id"]`` before FastMCP
    dispatches the request. We reach it via ``get_http_request()`` which reads
    from the mcp SDK's ``request_ctx`` (preferred) or the HTTP ContextVar set
    by the streamable-HTTP transport.

    Returns ``None`` if the middleware state is missing (should not happen in
    production because the middleware rejects unauthenticated requests, but
    can occur in unit-test scenarios where the middleware is bypassed).
    """
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        state = request.scope.get("state") or {}
        return state.get("mcp_principal_id")
    except Exception:
        return None


def _validate_id(value: str, field: str) -> dict | None:
    """Return a structured ``invalid_parameter`` error if *value* fails the regex.

    Returns ``None`` when the value is valid.
    """
    if not _INPUT_REGEX.match(value):
        return {"error": "invalid_parameter", "parameter": field}
    return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_mcp() -> FastMCP:
    """Construct the v2 FastMCP instance used by the ``/mcp`` mount.

    Auth is enforced ONE level up by the ASGI
    :class:`~beever_atlas.infra.mcp_auth.MCPAuthMiddleware` wrapped around the
    :py:meth:`fastmcp.FastMCP.http_app` output — by the time a tool handler
    runs, ``scope["state"]["mcp_principal_id"]`` is populated with the caller's
    ``mcp:<hash>`` principal id. Tool handlers read the principal via
    :func:`_get_principal_id` and call ``assert_channel_access`` /
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
    _register_discovery_tools(mcp)
    _register_retrieval_tools(mcp)
    _register_graph_tools(mcp)
    _register_session_tools(mcp)
    _register_orchestration_tools(mcp)
    _register_resources(mcp)
    _register_prompts(mcp)

    tool_count = sum(
        1 for k in mcp._local_provider._components if k.startswith("tool:")
    )
    logger.info(
        "event=mcp_build name=beever-atlas version=%s tools_registered=%d",
        _atlas_version(),
        tool_count,
    )
    return mcp


# ---------------------------------------------------------------------------
# Deprecation shim (Phase 2, retained permanently)
# ---------------------------------------------------------------------------


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
# Discovery tools (3.1 – 3.3)
# ---------------------------------------------------------------------------


def _register_discovery_tools(mcp: FastMCP) -> None:

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
            logger.warning(
                "event=mcp_tool_missing_principal tool=whoami"
            )
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

        Do NOT use to check channel access — use ``list_channels`` for that.

        Returns: ``{connections: [<connection dict>, ...]}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            logger.warning(
                "event=mcp_tool_missing_principal tool=list_connections"
            )
            return {"error": "authentication_missing"}

        try:
            from beever_atlas.capabilities import connections as conn_cap

            conns = await conn_cap.list_connections(principal_id)
            return {"connections": conns}
        except Exception:
            logger.exception(
                "list_connections: capability failed for principal=%s", principal_id
            )
            return {"connections": []}

    @mcp.tool(name="list_channels")
    async def list_channels(
        connection_id: Annotated[str, "The connection id to list channels for (from list_connections)"],
        ctx: Context,
    ) -> dict:
        """Return channels selected for sync on a specific connection you own.

        Each channel entry contains: ``channel_id``, ``name``, ``platform``,
        ``last_sync_ts``, ``sync_status``, and ``message_count_estimate``.

        When to use: after ``list_connections`` to see which specific channels
        (Slack channels, Discord channels) are available for querying under a
        given connection. Use the returned ``channel_id`` values with retrieval
        tools like ``ask_channel``, ``search_channel_facts``, etc.

        Raises a structured ``connection_access_denied`` error if the principal
        does not own the requested connection — connection existence is not leaked.

        Returns: ``{channels: [...]}`` or ``{error: "connection_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            logger.warning(
                "event=mcp_tool_missing_principal tool=list_channels"
            )
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


# ---------------------------------------------------------------------------
# Retrieval tools (3.4 – 3.5)
# ---------------------------------------------------------------------------


def _register_retrieval_tools(mcp: FastMCP) -> None:

    @mcp.tool(name="ask_channel", timeout=90.0)
    async def ask_channel(
        channel_id: Annotated[str, "The channel id to query (from list_channels)"],
        question: Annotated[str, "The natural-language question to answer"],
        ctx: Context,
        mode: Annotated[str, "QA mode: 'quick' (fast BM25), 'deep' (full ADK pipeline), or 'summarize'"] = "deep",
        session_id: Annotated[str | None, "Session id for conversation continuity; defaults to a per-principal session"] = None,
    ) -> dict:
        """Answer a natural-language question about a channel's knowledge base.

        This is the FLAGSHIP retrieval tool. It invokes the full ADK QA pipeline
        (embeddings + BM25 hybrid search + graph context + optional multi-hop
        reasoning) and returns a structured answer with citations.

        When to use: whenever the user asks a question about channel content,
        wants cited facts, or needs reasoning across multiple messages.
        Prefer ``search_channel_facts`` for exact keyword search without inference.

        mode options:
        - ``"quick"``: fast BM25-only retrieval, no ADK reasoning, ~3s
        - ``"deep"``: full ADK pipeline with graph context, ~20–60s (default)
        - ``"summarize"``: structured summary with wiki pages, ~10–30s

        The tool enforces a 90-second hard cap. On timeout, returns
        ``{error: "answer_timeout"}``. On channel access denial, returns
        ``{error: "channel_access_denied"}``.

        Returns: ``{answer, citations, follow_ups, metadata}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            logger.warning(
                "event=mcp_tool_missing_principal tool=ask_channel"
            )
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        # Phase 3 stub: ADK runner integration is deferred to Phase 3b / Phase 9.
        # The stub returns a structured not_implemented payload so release-gate
        # tests can detect the gap explicitly rather than seeing a runtime crash.
        logger.warning(
            "event=mcp_ask_channel_stub channel_id=%s principal=%s "
            "detail='ADK runner not yet wired; Phase 9 gate requires real implementation'",
            channel_id,
            principal_id,
        )

        # Still perform the channel access check so access-denied errors work correctly.
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException) and exc.status_code == 403:
                return {"error": "channel_access_denied", "channel_id": channel_id}
            # Other errors (e.g. store unavailable) — fall through to stub response.

        effective_session_id = session_id or f"mcp:{principal_id}"
        await ctx.info(
            f"ask_channel stub: channel={channel_id} session={effective_session_id} "
            f"(ADK runner integration pending Phase 3b)"
        )

        return {
            "error": "not_implemented_in_phase3",
            "detail": (
                "ask_channel requires Phase 3b ADK runner integration. "
                "The tool is registered and channel access is enforced, "
                "but the QA pipeline is not yet wired."
            ),
        }

    @mcp.tool(name="search_channel_facts")
    async def search_channel_facts(
        channel_id: Annotated[str, "The channel id to search (from list_channels)"],
        query: Annotated[str, "Search query — BM25+vector hybrid over atomic facts"],
        ctx: Context,
        time_scope: Annotated[str, "'any' (all time) or 'recent' (last 30 days)"] = "any",
        limit: Annotated[int, "Maximum number of facts to return (1–50)"] = 10,
    ) -> dict:
        """Search atomic facts stored from a channel using BM25+vector hybrid retrieval.

        Each returned fact includes ``text``, ``author``, ``timestamp``,
        ``permalink``, ``channel_id``, ``confidence``, and ``topic_tags``.

        When to use: for targeted keyword or semantic search when you need
        specific facts with citations. Faster and more precise than ``ask_channel``
        for lookup queries. Use ``ask_channel`` when you need synthesized answers
        with reasoning across multiple facts.

        time_scope: ``"any"`` returns all facts; ``"recent"`` restricts to the
        last 30 days. Default: ``"any"``.

        Returns: ``{facts: [...]}`` or ``{error: "channel_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import memory as mem_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            facts = await mem_cap.search_channel_facts(
                principal_id, channel_id, query,
                time_scope=time_scope, limit=limit,
            )
            return {"facts": facts}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "search_channel_facts: failed principal=%s channel_id=%s",
                principal_id, channel_id,
            )
            return {"facts": []}

    @mcp.tool(name="get_wiki_page")
    async def get_wiki_page(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        ctx: Context,
        page_type: Annotated[str, "Wiki page type: overview, faq, decisions, people, glossary, activity, topics"] = "overview",
    ) -> dict:
        """Retrieve a pre-compiled wiki page for a channel.

        Wiki pages are generated offline during the sync pipeline and contain
        summarised, structured knowledge: ``overview`` (channel purpose and key
        topics), ``faq`` (common questions), ``decisions`` (key decisions made),
        ``people`` (active contributors), ``glossary`` (domain terms), and more.

        When to use: for quick structured summaries without invoking the full QA
        pipeline. Faster than ``ask_channel`` but less precise for specific
        queries. Use ``ask_channel`` when the wiki page doesn't have the answer.

        Returns the page dict verbatim (``page_type``, ``channel_id``,
        ``content``, ``summary``, ``text``), or ``null`` if the page has not
        been generated yet, or ``{error: "channel_access_denied"}`` on denial.
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import wiki as wiki_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            page = await wiki_cap.get_wiki_page(principal_id, channel_id, page_type)
            return page if page is not None else {"page_type": page_type, "channel_id": channel_id, "content": None}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "get_wiki_page: failed principal=%s channel_id=%s page_type=%s",
                principal_id, channel_id, page_type,
            )
            return {"page_type": page_type, "channel_id": channel_id, "content": None}

    @mcp.tool(name="get_recent_activity")
    async def get_recent_activity(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        ctx: Context,
        days: Annotated[int, "Look-back window in days (1–90)"] = 7,
        topic: Annotated[str | None, "Optional topic filter — narrows search to facts related to this topic"] = None,
        limit: Annotated[int, "Maximum number of activity items to return (1–50)"] = 20,
    ) -> dict:
        """Return the most recent activity from a channel, optionally filtered by topic.

        Results are sorted by timestamp descending and include ``text``,
        ``author``, ``timestamp``, ``channel_id``, ``topic_tags``, and ``fact_id``.

        When to use: to answer "what has been discussed recently in #channel?"
        or "what happened with topic X in the last N days?" Use ``ask_channel``
        when you need reasoning or synthesis across multiple activity items.
        Use ``search_channel_facts`` for non-time-bounded search.

        Returns: ``{activity: [...]}`` or ``{error: "channel_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import memory as mem_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            activity = await mem_cap.get_recent_activity(
                principal_id, channel_id, days=days, topic=topic, limit=limit,
            )
            return {"activity": activity}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "get_recent_activity: failed principal=%s channel_id=%s",
                principal_id, channel_id,
            )
            return {"activity": []}

    @mcp.tool(name="search_media_references")
    async def search_media_references(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        query: Annotated[str, "Search query for finding media-containing messages"],
        ctx: Context,
        media_type: Annotated[str | None, "Filter by media type: 'image', 'pdf', 'link', or null for all"] = None,
        limit: Annotated[int, "Maximum number of results to return (1–20)"] = 5,
    ) -> dict:
        """Search for messages containing images, PDFs, or links shared in a channel.

        Each result includes ``text``, ``media_urls``, ``link_urls``,
        ``link_titles``, ``author``, ``timestamp``, ``media_type``, and
        ``fact_id``.

        When to use: when the user asks about documents, images, or links shared
        in a channel, or when you need to find a specific file or URL. Do NOT use
        for general knowledge search — use ``search_channel_facts`` for that.

        media_type: ``"image"`` (photos/screenshots), ``"pdf"`` (documents),
        ``"link"`` (URLs), or ``null`` (all types). Default: ``null``.

        Returns: ``{media: [...]}`` or ``{error: "channel_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import memory as mem_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            media = await mem_cap.search_media_references(
                principal_id, channel_id, query, media_type=media_type, limit=limit,
            )
            return {"media": media}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "search_media_references: failed principal=%s channel_id=%s",
                principal_id, channel_id,
            )
            return {"media": []}


# ---------------------------------------------------------------------------
# Graph tools (3.6)
# ---------------------------------------------------------------------------


def _register_graph_tools(mcp: FastMCP) -> None:

    @mcp.tool(name="find_experts")
    async def find_experts(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        topic: Annotated[str, "Topic or keyword to find subject-matter experts for"],
        ctx: Context,
        limit: Annotated[int, "Maximum number of experts to return (1–20)"] = 5,
    ) -> dict:
        """Identify the most knowledgeable people about a topic in a channel.

        Scores channel members by graph-edge frequency for the topic and returns
        a ranked list. Each entry includes ``handle``, ``expertise_score``,
        ``fact_count``, and ``top_topics``.

        When to use: to answer "who knows the most about X in #channel?" or
        to find the right person to ask for a specific domain. Use
        ``search_channel_facts`` to find facts, not people.

        Returns: ``{experts: [...]}`` or ``{error: "channel_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import graph as graph_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            experts = await graph_cap.find_experts(principal_id, channel_id, topic, limit=limit)
            return {"experts": experts}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "find_experts: failed principal=%s channel_id=%s topic=%s",
                principal_id, channel_id, topic,
            )
            return {"experts": []}

    @mcp.tool(name="search_relationships")
    async def search_relationships(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        entities: Annotated[list[str], "List of entity names to find relationships for"],
        ctx: Context,
        hops: Annotated[int, "Number of graph hops to traverse (1–4)"] = 2,
    ) -> dict:
        """Traverse the knowledge graph to find relationships between entities.

        Returns a subgraph of nodes and edges connecting the requested entities.
        Each node has ``name`` and ``type``; each edge has ``source``, ``target``,
        ``type``, ``confidence``, and ``context``.

        When to use: to answer "how is X related to Y?" or to explore entity
        connections in a channel's knowledge graph. Use ``find_experts`` to
        find people, not relationships.

        Returns: ``{nodes, edges, text, entities_searched}`` or ``{error: ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import graph as graph_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            result = await graph_cap.search_relationships(
                principal_id, channel_id, entities, hops=hops
            )
            if isinstance(result, dict):
                return result
            return {"edges": result, "channel_id": channel_id}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "search_relationships: failed principal=%s channel_id=%s",
                principal_id, channel_id,
            )
            return {"nodes": [], "edges": [], "channel_id": channel_id}

    @mcp.tool(name="trace_decision_history")
    async def trace_decision_history(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        topic: Annotated[str, "Topic or decision to trace (e.g. 'database choice', 'API versioning')"],
        ctx: Context,
    ) -> dict:
        """Trace the history of decisions made about a topic in a channel.

        Follows ``SUPERSEDES`` edges in the knowledge graph to reconstruct
        the decision timeline. Each item includes ``entity``, ``superseded_by``,
        ``relationship``, ``confidence``, ``context``, and ``position``.

        When to use: to answer "how did the team arrive at the current approach
        for X?" or "what earlier decisions were overridden?" Use
        ``search_channel_facts`` to find facts about the current state without
        historical context.

        Returns: ``{decisions: [...]}`` or ``{error: "channel_access_denied", ...}``
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import graph as graph_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            decisions = await graph_cap.trace_decision_history(principal_id, channel_id, topic)
            if isinstance(decisions, list):
                return {"decisions": decisions}
            return decisions  # type: ignore[return-value]
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "trace_decision_history: failed principal=%s channel_id=%s topic=%s",
                principal_id, channel_id, topic,
            )
            return {"decisions": []}


# ---------------------------------------------------------------------------
# Session tools (3.7)
# ---------------------------------------------------------------------------


def _register_session_tools(mcp: FastMCP) -> None:

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


# ---------------------------------------------------------------------------
# Orchestration tools (5.2 – 5.4)
# ---------------------------------------------------------------------------


def _register_orchestration_tools(mcp: FastMCP) -> None:
    """Register long-running-job tools: trigger_sync, refresh_wiki, get_job_status.

    These tools wrap the framework-neutral capabilities in
    ``beever_atlas.capabilities.{sync,wiki,jobs}`` with the standard
    principal-extraction, validation, and error-translation patterns used by
    the rest of the tool catalog.
    """

    @mcp.tool(
        name="trigger_sync",
        description=(
            "Trigger an incremental or full sync of a channel's messages into the "
            "Atlas knowledge base. Returns a job envelope "
            "{job_id, status_uri, status: 'queued'} within 5 seconds; the actual "
            "ingestion runs in the background. Poll atlas://job/<job_id> or call "
            "get_job_status to track progress.\n\n"
            "IMPORTANT — only call when the user EXPLICITLY asks to refresh or sync "
            "a channel, OR when retrieval tools return empty/stale results AND the "
            "channel's last_sync_ts is beyond 24 hours ago. Otherwise prefer existing "
            "indexed data — sync is expensive and rate-limited to 5/min per principal. "
            "Do NOT call before every question or as a precautionary step.\n\n"
            "If a queued or running sync job already exists for the same channel, "
            "the existing job_id is returned (idempotent). A new job is only created "
            "when no active job exists, or after a previous job has completed or failed.\n\n"
            "sync_type: 'incremental' (default — fetches only new messages since last "
            "sync), 'full' (re-fetches all messages; expensive), or 'auto' (server "
            "chooses based on sync history)."
        ),
    )
    async def trigger_sync(
        channel_id: Annotated[str, "The channel id to sync (from list_channels)"],
        ctx: Context,
        sync_type: Annotated[str, "Sync mode: 'incremental' (default), 'full', or 'auto'"] = "incremental",
    ) -> dict:
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import sync as sync_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied, CooldownActive

            result = await sync_cap.trigger_sync(principal_id, channel_id, sync_type=sync_type)
            return result
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except CooldownActive as exc:
            return {"error": "cooldown_active", "retry_after_seconds": exc.retry_after_seconds}
        except Exception:
            logger.exception(
                "trigger_sync: capability failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"error": "internal_error", "channel_id": channel_id}

    @mcp.tool(
        name="refresh_wiki",
        description=(
            "Regenerate pre-compiled wiki pages for a channel from its ingested facts. "
            "Returns a job envelope {job_id, status_uri, status: 'queued'} within 5 "
            "seconds; generation runs in the background.\n\n"
            "Expensive — only call after a fresh sync has added new facts (i.e., after "
            "trigger_sync completes), or when the user explicitly requests wiki "
            "regeneration. Do NOT call routinely — wiki pages are rebuilt automatically "
            "as part of the standard sync pipeline.\n\n"
            "page_types: optional subset of page types to regenerate. Valid values: "
            "overview, faq, decisions, people, glossary, activity, topics. "
            "Omit to regenerate all pages."
        ),
    )
    async def refresh_wiki(
        channel_id: Annotated[str, "The channel id to regenerate wiki pages for (from list_channels)"],
        ctx: Context,
        page_types: Annotated[
            list[str] | None,
            "Optional list of page types to regenerate: overview, faq, decisions, people, glossary, activity, topics",
        ] = None,
    ) -> dict:
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import wiki as wiki_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied, CooldownActive

            result = await wiki_cap.refresh_wiki(principal_id, channel_id, page_types=page_types)
            return result
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except CooldownActive as exc:
            return {"error": "cooldown_active", "retry_after_seconds": exc.retry_after_seconds}
        except Exception:
            logger.exception(
                "refresh_wiki: capability failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"error": "internal_error", "channel_id": channel_id}

    @mcp.tool(
        name="get_job_status",
        description=(
            "Poll the state of a long-running job created by trigger_sync or "
            "refresh_wiki. Returns a structured dict: "
            "{job_id, kind, status, progress, started_at, updated_at, ended_at, "
            "result, error, target}.\n\n"
            "status values: queued, running, done, error, cancelled.\n"
            "progress: fraction 0.0–1.0 or null when not yet available.\n\n"
            "Returns {error: 'job_not_found', job_id: ...} for jobs that do not "
            "exist or are not owned by the calling principal — no information about "
            "other principals' jobs is disclosed.\n\n"
            "Use atlas://job/<id> as a resource-read alternative when your MCP "
            "client prefers resources/read over tool calls."
        ),
    )
    async def get_job_status(
        job_id: Annotated[str, "The job id returned by trigger_sync or refresh_wiki"],
        ctx: Context,
    ) -> dict:
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(job_id, "job_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import jobs as jobs_cap
            from beever_atlas.capabilities.errors import JobNotFound

            status = await jobs_cap.get_job_status(principal_id, job_id)
            return status
        except JobNotFound:
            return {"error": "job_not_found", "job_id": job_id}
        except Exception:
            logger.exception(
                "get_job_status: capability failed principal=%s job_id=%s",
                principal_id,
                job_id,
            )
            return {"error": "job_not_found", "job_id": job_id}


# ---------------------------------------------------------------------------
# Resources (4.1 – 4.5)
# ---------------------------------------------------------------------------


def _register_resources(mcp: FastMCP) -> None:
    """Register all atlas:// URI resources.

    Resources with {param} in the URI are automatically registered as
    ResourceTemplates by FastMCP 3.x — the framework extracts param values
    from the URI and passes them as function kwargs.
    """

    # 4.1 atlas://connection/{connection_id}
    @mcp.resource(
        "atlas://connection/{connection_id}",
        name="connection",
        description="Metadata for a single platform connection owned by the calling MCP principal.",
        mime_type="application/json",
    )
    async def get_connection(connection_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(connection_id, "connection_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import connections as conn_cap
            from beever_atlas.capabilities.errors import ConnectionAccessDenied

            conns = await conn_cap.list_connections(principal_id)
            for conn in conns:
                if conn.get("connection_id") == connection_id:
                    return conn
            return {"error": "connection_not_found", "connection_id": connection_id}
        except ConnectionAccessDenied:
            return {"error": "connection_access_denied", "connection_id": connection_id}
        except Exception:
            logger.exception(
                "resource get_connection: failed principal=%s connection_id=%s",
                principal_id, connection_id,
            )
            return {"error": "internal_error", "connection_id": connection_id}

    # 4.2 atlas://connection/{connection_id}/channels
    @mcp.resource(
        "atlas://connection/{connection_id}/channels",
        name="connection-channels",
        description="All channels selected for sync under a connection owned by the calling principal.",
        mime_type="application/json",
    )
    async def get_connection_channels(connection_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(connection_id, "connection_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import connections as conn_cap
            from beever_atlas.capabilities.errors import ConnectionAccessDenied

            channels = await conn_cap.list_channels(principal_id, connection_id)
            return {"channels": channels, "connection_id": connection_id}
        except ConnectionAccessDenied:
            return {"error": "connection_access_denied", "connection_id": connection_id}
        except Exception:
            logger.exception(
                "resource get_connection_channels: failed principal=%s connection_id=%s",
                principal_id, connection_id,
            )
            return {"error": "internal_error", "connection_id": connection_id}

    # 4.3 atlas://channel/{channel_id}/wiki — wiki structure index
    @mcp.resource(
        "atlas://channel/{channel_id}/wiki",
        name="channel-wiki-index",
        description=(
            "Wiki structure index for a channel: overview summary and available page types. "
            "Returns a stub if the wiki cache has not been populated yet."
        ),
        mime_type="application/json",
    )
    async def get_channel_wiki_index(channel_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import wiki as wiki_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            overview = await wiki_cap.get_topic_overview(principal_id, channel_id)
            if overview is None:
                # Return structured stub — wiki not generated yet.
                logger.warning(
                    "event=mcp_resource_wiki_stub channel_id=%s "
                    "detail='wiki structure index awaits Phase 6 wiki-cache integration'",
                    channel_id,
                )
                return {
                    "channel_id": channel_id,
                    "page_types": list(wiki_cap.SUPPORTED_PAGE_TYPES),
                    "overview": None,
                    "stub": True,
                }
            return {
                "channel_id": channel_id,
                "page_types": list(wiki_cap.SUPPORTED_PAGE_TYPES),
                "overview": overview,
                "stub": False,
            }
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "resource get_channel_wiki_index: failed principal=%s channel_id=%s",
                principal_id, channel_id,
            )
            return {"error": "internal_error", "channel_id": channel_id}

    # 4.4 atlas://channel/{channel_id}/wiki/page/{page_id}
    @mcp.resource(
        "atlas://channel/{channel_id}/wiki/page/{page_id}",
        name="channel-wiki-page",
        description=(
            "Pre-compiled wiki page content for a channel. page_id is one of: "
            "overview, faq, decisions, people, glossary, activity, topics."
        ),
        mime_type="application/json",
    )
    async def get_channel_wiki_page(channel_id: str, page_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id") or _validate_id(page_id, "page_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import wiki as wiki_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            page = await wiki_cap.get_wiki_page(principal_id, channel_id, page_type=page_id)
            if page is None:
                return {
                    "channel_id": channel_id,
                    "page_type": page_id,
                    "content": None,
                    "generated_at": None,
                    "citations": [],
                }
            return {
                **page,
                "generated_at": None,  # Phase 6 will wire real timestamps
                "citations": [],
            }
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "resource get_channel_wiki_page: failed principal=%s channel_id=%s page_id=%s",
                principal_id, channel_id, page_id,
            )
            return {"error": "internal_error", "channel_id": channel_id, "page_type": page_id}

    # 4.5 atlas://job/{job_id}
    @mcp.resource(
        "atlas://job/{job_id}",
        name="job-status",
        description=(
            "Status of a long-running sync or wiki-refresh job. Returns job_not_found "
            "for jobs not owned by the calling principal (no information leak)."
        ),
        mime_type="application/json",
    )
    async def get_job(job_id: str) -> dict:
        principal_id = _get_principal_id_from_resource()
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(job_id, "job_id")
        if err:
            return err

        try:
            from beever_atlas.capabilities import jobs as jobs_cap
            from beever_atlas.capabilities.errors import JobNotFound

            status = await jobs_cap.get_job_status(principal_id, job_id)
            return status
        except JobNotFound:
            return {"error": "job_not_found", "job_id": job_id}
        except Exception:
            logger.exception(
                "resource get_job: failed principal=%s job_id=%s",
                principal_id, job_id,
            )
            return {"error": "job_not_found", "job_id": job_id}


def _get_principal_id_from_resource() -> str | None:
    """Extract ``mcp_principal_id`` inside a resource handler.

    Resource handlers run in the same ASGI request context as tool handlers;
    the MCPAuthMiddleware has already injected the principal into
    ``scope["state"]["mcp_principal_id"]``. We reuse :func:`_get_principal_id`
    which reads from ``get_http_request()``.
    """
    try:
        from fastmcp.server.dependencies import get_http_request

        request = get_http_request()
        state = request.scope.get("state") or {}
        return state.get("mcp_principal_id")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Prompts (4.6 – 4.8)
# ---------------------------------------------------------------------------


def _register_prompts(mcp: FastMCP) -> None:
    """Register static prompt templates for common Atlas workflows.

    Prompt handlers are pure template builders — they receive only the
    declared parameters and return a filled message array. No database
    access or capability calls are made inside prompt handlers.
    """

    # 4.6 summarize_channel
    @mcp.prompt(name="summarize_channel")
    def summarize_channel(channel_id: str, since_days: int = 7) -> list[dict]:
        """Produce a user-role instruction to summarize recent channel discussion.

        Parameters:
            channel_id: The channel id to summarize (from list_channels).
            since_days: Look-back window in days (default 7).

        Returns a filled message array telling the LLM which tools to use and
        what to focus on (decisions, open questions, key participants).
        """
        return [
            {
                "role": "user",
                "content": (
                    f"Summarize the last {since_days} days of discussion in #{channel_id}. "
                    "Focus on decisions made, open questions, and key participants. "
                    "Use get_wiki_page(page_type='activity') and get_recent_activity "
                    "to ground your answer."
                ),
            }
        ]

    # 4.7 investigate_decision
    @mcp.prompt(name="investigate_decision")
    def investigate_decision(channel_id: str, topic: str) -> list[dict]:
        """Produce a decision-trace-style instruction for investigating a topic.

        Parameters:
            channel_id: The channel id to investigate (from list_channels).
            topic: The decision or topic to trace (e.g. 'database choice').

        Returns a filled message array telling the LLM to follow SUPERSEDES
        chains, identify decision drivers, and ground claims with fact search.
        """
        return [
            {
                "role": "user",
                "content": (
                    f"Trace the decision history for '{topic}' in channel {channel_id}. "
                    "Use trace_decision_history for the SUPERSEDES chain, find_experts "
                    "to identify who drove the decision, and search_channel_facts to "
                    "ground individual claims."
                ),
            }
        ]

    # 4.8 onboard_new_channel
    @mcp.prompt(name="onboard_new_channel")
    def onboard_new_channel(channel_id: str) -> list[dict]:
        """Produce an onboarding-overview instruction for a new channel.

        Parameters:
            channel_id: The channel id to onboard (from list_channels).

        Returns a filled message array telling the LLM to read the overview,
        people, and topics wiki pages and synthesize an orientation summary.
        """
        return [
            {
                "role": "user",
                "content": (
                    f"Give me an onboarding overview of channel {channel_id}. "
                    "Call get_wiki_page(page_type='overview') first, then "
                    "get_wiki_page(page_type='people') and (page_type='topics'). "
                    "Summarize scope, key people, active topics, and recent decisions."
                ),
            }
        ]


__all__ = ["build_mcp"]
