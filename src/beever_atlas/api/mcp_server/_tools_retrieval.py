"""Retrieval tools: ask_channel, search_channel_facts, get_wiki_page,
get_recent_activity, search_media_references (Phase 3, tasks 3.4–3.5)."""

from __future__ import annotations

import logging
from typing import Annotated

from fastmcp import Context, FastMCP

from beever_atlas.api.mcp_server._helpers import (
    _get_principal_id,
    _validate_id,
)

logger = logging.getLogger(__name__)


def register_retrieval_tools(mcp: FastMCP) -> None:

    @mcp.tool(name="ask_channel", timeout=90.0)
    async def ask_channel(
        channel_id: Annotated[str, "The channel id to query (from list_channels)"],
        question: Annotated[str, "The natural-language question to answer"],
        ctx: Context,
        mode: Annotated[
            str,
            "QA mode: 'quick' (fast BM25), 'deep' (full ADK pipeline), or 'summarize'",
        ] = "deep",
        session_id: Annotated[
            str | None,
            "Session id for conversation continuity; defaults to a per-principal session",
        ] = None,
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
            logger.warning("event=mcp_tool_missing_principal tool=ask_channel")
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        # Cost / availability guard: cap question length so a caller cannot
        # submit a megabyte prompt that burns the Gemini quota and holds a
        # 90s worker. 4KB is ample for natural-language questions.
        if not question or len(question) > 4000:
            return {
                "error": "invalid_parameter",
                "parameter": "question",
                "detail": "length must be 1..4000 characters",
            }

        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException) and exc.status_code == 403:
                return {"error": "channel_access_denied", "channel_id": channel_id}
            # Any other exception surfaces as an adk_error — the caller sees a
            # structured dict rather than a protocol-level failure.
            logger.warning(
                "event=mcp_ask_channel_access_check_failed channel=%s err=%r",
                channel_id,
                exc,
            )
            return {"error": "channel_access_denied", "channel_id": channel_id}

        if mode not in {"quick", "summarize", "deep"}:
            return {
                "error": "invalid_parameter",
                "parameter": "mode",
                "detail": "mode must be one of: quick, summarize, deep",
            }

        import asyncio

        from beever_atlas.api.mcp_server._ask_runner import run_ask_channel

        try:
            return await run_ask_channel(
                principal_id=principal_id,
                channel_id=channel_id,
                question=question,
                mode=mode,
                session_id=session_id,
                ctx=ctx,
            )
        except asyncio.TimeoutError:
            logger.info(
                "event=mcp_ask_channel_timeout channel=%s principal=%s",
                channel_id,
                principal_id,
            )
            return {"error": "answer_timeout"}
        except Exception:
            # Never surface raw exception details to MCP clients — they may
            # contain internal hostnames, quota-project ids, or stack
            # fragments. Full traceback is in the server log instead.
            logger.exception(
                "event=mcp_ask_channel_runner_error channel=%s principal=%s",
                channel_id,
                principal_id,
            )
            return {"error": "adk_error"}

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

        # Fix #8: clamp to the documented 1–50 bound server-side so a
        # misbehaving client cannot burn retrieval cost with limit=999.
        limit = max(1, min(limit, 50))

        try:
            from beever_atlas.capabilities import memory as mem_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            facts = await mem_cap.search_channel_facts(
                principal_id,
                channel_id,
                query,
                time_scope=time_scope,
                limit=limit,
            )
            return {"facts": facts}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "search_channel_facts: failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"facts": []}

    @mcp.tool(name="get_wiki_page")
    async def get_wiki_page(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        ctx: Context,
        page_type: Annotated[
            str,
            "Wiki page type: overview, faq, decisions, people, glossary, activity, topics",
        ] = "overview",
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
            return (
                page
                if page is not None
                else {
                    "page_type": page_type,
                    "channel_id": channel_id,
                    "content": None,
                }
            )
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "get_wiki_page: failed principal=%s channel_id=%s page_type=%s",
                principal_id,
                channel_id,
                page_type,
            )
            return {"page_type": page_type, "channel_id": channel_id, "content": None}

    @mcp.tool(name="get_recent_activity")
    async def get_recent_activity(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        ctx: Context,
        days: Annotated[int, "Look-back window in days (1–90)"] = 7,
        topic: Annotated[
            str | None,
            "Optional topic filter — narrows search to facts related to this topic",
        ] = None,
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

        # Fix #8: clamp to documented ranges.
        days = max(1, min(days, 90))
        limit = max(1, min(limit, 50))

        try:
            from beever_atlas.capabilities import memory as mem_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            activity = await mem_cap.get_recent_activity(
                principal_id, channel_id, days=days, topic=topic, limit=limit
            )
            return {"activity": activity}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "get_recent_activity: failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"activity": []}

    @mcp.tool(name="search_media_references")
    async def search_media_references(
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        query: Annotated[str, "Search query for finding media-containing messages"],
        ctx: Context,
        media_type: Annotated[
            str | None,
            "Filter by media type: 'image', 'pdf', 'link', or null for all",
        ] = None,
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

        # Fix #8: clamp limit to documented 1–20 bound.
        limit = max(1, min(limit, 20))

        try:
            from beever_atlas.capabilities import memory as mem_cap
            from beever_atlas.capabilities.errors import ChannelAccessDenied

            media = await mem_cap.search_media_references(
                principal_id,
                channel_id,
                query,
                media_type=media_type,
                limit=limit,
            )
            return {"media": media}
        except ChannelAccessDenied:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        except Exception:
            logger.exception(
                "search_media_references: failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"media": []}
