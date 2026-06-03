"""Retrieval + wiki tools (17): ask_channel, search_channel_facts, get_wiki_page,
get_recent_activity, search_media_references, search_memory, lint_wiki,
get_extraction_status, read_wiki_page, list_wiki_pages, get_wiki_graph,
read_wiki_module, find_decisions, get_tensions, find_facts, read_wiki_section,
read_provenance."""

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
        channel_id: Annotated[
            str,
            "Channel id to query. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        question: Annotated[
            str,
            "Natural-language question, 1-4000 chars (e.g. 'What database did we pick "
            "and why?'). Longer questions return error 'invalid_parameter'. Required.",
        ],
        ctx: Context,
        mode: Annotated[
            str,
            "Retrieval depth. One of: 'quick' (BM25 only, no reasoning, ~3s), "
            "'deep' (full pipeline with graph + multi-hop reasoning, ~20-60s), "
            "'summarize' (structured summary, ~10-30s). Default 'deep'.",
        ] = "deep",
        session_id: Annotated[
            str | None,
            "Optional session id for multi-turn continuity (e.g. 'sess-abc123' from "
            "start_new_session). Omit for a per-principal default session. Default null.",
        ] = None,
    ) -> dict:
        """Answer a natural-language QUESTION about one channel with synthesized,
        cited reasoning. The flagship retrieval tool — call it when the user asks
        anything that needs an ANSWER rather than raw rows.

        When to use: any question about a channel's content where you want a
        composed answer with citations and reasoning across multiple messages
        ("what did we decide about X", "why did the project slip").

        When NOT to use: exact keyword/semantic lookup of individual facts (use
        search_channel_facts); cross-channel recall when you don't know which
        channel holds the answer (use search_memory); a deterministic substring
        scan (use find_facts). Those are faster and return raw rows, not prose.

        Prerequisites: a channel_id from list_channels.

        Returns (instant for 'quick', long-running up to a 90s hard cap for
        'deep'/'summarize'): a dict
        ``{answer: str, citations: [{fact_id, text, permalink, author, ts}],
        follow_ups: [str], metadata: {mode, ...}}``. Read-only — no side effects,
        triggers no jobs.

        Error modes (all returned as ``{error: ...}`` dicts, never exceptions):
        'authentication_missing' (no principal); 'invalid_parameter' (empty/over-
        4000-char question, or mode not in quick/deep/summarize);
        'channel_access_denied' (token lacks access to channel_id);
        'answer_timeout' (exceeded the 90s cap — retry with mode='quick');
        'adk_error' (internal pipeline failure).
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
        channel_id: Annotated[
            str,
            "Channel id to search. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        query: Annotated[
            str,
            "Search query, keyword or natural phrase (e.g. 'postgres migration'). "
            "Matched with BM25+vector hybrid retrieval. Required.",
        ],
        ctx: Context,
        time_scope: Annotated[
            str,
            "Time window. 'any' = all facts (default), 'recent' = last 30 days only.",
        ] = "any",
        limit: Annotated[
            int,
            "Max facts to return, 1-50 (values outside the range are clamped). Default 10.",
        ] = 10,
    ) -> dict:
        """Find SPECIFIC facts in ONE channel by hybrid (BM25 + vector) search and
        return them as raw, ranked rows. Call it when you want the cited source
        facts themselves, not a composed answer.

        When to use: targeted lookup of facts in a known channel ("find facts
        about the postgres migration"). Faster and more precise than ask_channel
        for retrieval-only tasks.

        When NOT to use: you need a synthesized answer with reasoning (use
        ask_channel); you don't know which channel holds the facts (use
        search_memory, which fans this same search across every accessible
        channel); you want a deterministic substring match rather than ranked
        relevance (use find_facts).

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): ``{facts: [{text, author, timestamp,
        permalink, channel_id, confidence, topic_tags}, ...]}``. No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id). On any other
        internal failure it returns an empty ``{facts: []}`` rather than erroring.
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
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        ctx: Context,
        page_type: Annotated[
            str,
            "Which fixed page to fetch. One of exactly: 'overview', 'faq', "
            "'decisions', 'people', 'glossary', 'activity', 'topics'. "
            "Default 'overview'.",
        ] = "overview",
    ) -> dict:
        """Fetch one pre-compiled wiki page from the LEGACY fixed-page set (overview,
        faq, decisions, people, glossary, activity, topics). Call it for a fast,
        whole-channel summary keyed by a fixed page_type.

        Disambiguation: this is the legacy fixed-page surface. For the redesigned
        slug-keyed wiki — arbitrary topic/entity pages, structured kind payloads,
        and the cross-link graph — use list_wiki_pages to discover pages then
        read_wiki_page(slug=...). Prefer those for anything beyond the seven
        fixed pages above.

        When to use: you want a quick structured summary of a known aspect of a
        channel without running the QA pipeline. When NOT to use: you need a
        specific answer (use ask_channel) or a non-fixed wiki topic (use
        read_wiki_page).

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): the page dict
        ``{page_type, channel_id, content, summary, text}``. ``content`` is null
        when that page has not been generated yet (run a sync / refresh_wiki
        first). No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id). Other internal
        failures return the page dict with ``content: null``.
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
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        ctx: Context,
        days: Annotated[
            int,
            "Look-back window in days, 1-90 (out-of-range values are clamped). Default 7.",
        ] = 7,
        topic: Annotated[
            str | None,
            "Optional topic filter (e.g. 'deployment'); keeps only facts tagged "
            "with that topic. Omit for all topics. Default null.",
        ] = None,
        limit: Annotated[
            int,
            "Max activity items, 1-50 (out-of-range values are clamped). Default 20.",
        ] = 20,
    ) -> dict:
        """List the most RECENT facts from one channel, newest first, optionally
        scoped to a topic. Call it for time-bounded "what happened lately"
        questions.

        When to use: "what's been discussed in this channel this week", "what
        happened with topic X in the last N days". When NOT to use: search not
        bounded by recency (use search_channel_facts); a synthesized answer or
        reasoning across the items (use ask_channel).

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): ``{activity: [{text, author, timestamp,
        channel_id, topic_tags, fact_id}, ...]}`` sorted by timestamp descending.
        No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id). Other internal
        failures return an empty ``{activity: []}``.
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
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        query: Annotated[
            str,
            "Search query describing the media you want (e.g. 'architecture "
            "diagram' or 'pricing pdf'). Required.",
        ],
        ctx: Context,
        media_type: Annotated[
            str | None,
            "Optional media-type filter. One of: 'image' (photos/screenshots), "
            "'pdf' (documents), 'link' (URLs), or null for all. Default null.",
        ] = None,
        limit: Annotated[
            int,
            "Max results, 1-20 (out-of-range values are clamped). Default 5.",
        ] = 5,
    ) -> dict:
        """Find messages that SHARED a file, image, PDF, or link in one channel.
        Call it when the user is hunting for an attachment or URL ("where's the
        design doc", "find the screenshot Alice posted") rather than for the
        knowledge in the text.

        When to use: locating shared documents, images, or links. When NOT to
        use: general fact/knowledge search (use search_channel_facts or
        ask_channel) — this tool only returns messages that carry media.

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): ``{media: [{text, media_urls, link_urls,
        link_titles, author, timestamp, media_type, fact_id}, ...]}``. No side
        effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id). Other internal
        failures return an empty ``{media: []}``.
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

    # -- search_memory ------------------------------------------------------
    @mcp.tool(name="search_memory")
    async def search_memory(
        query: Annotated[
            str,
            "Search query, keyword or natural phrase, 1-4000 chars (e.g. 'who owns "
            "billing'). Longer queries return error 'invalid_parameter'. Required.",
        ],
        ctx: Context,
        scope: Annotated[
            str,
            "Search scope. 'all' (default) = every channel the principal can "
            "access; 'channel:<id>' = one channel only (e.g. 'channel:ch-eng').",
        ] = "all",
        limit: Annotated[
            int,
            "Max hits across the merged result set, 1-50 (out-of-range values are "
            "clamped). Default 20.",
        ] = 20,
    ) -> dict:
        """Find facts ACROSS MANY channels by hybrid (BM25 + vector) search when you
        do NOT know which channel holds the answer. Call it first for broad recall,
        then drill into a specific channel with the tools below.

        Routing rule for the three search tools:
        - search_memory(scope='all') — unknown channel; fans the search across
          every channel the principal can access and merges/ranks the hits.
        - search_channel_facts(channel_id) — known channel; same hybrid search,
          scoped to one channel, returning the richer per-fact shape.
        - search_memory(scope='channel:<id>') — single channel with the
          search_memory hit shape (use search_channel_facts instead if you want
          author/permalink/topic_tags on each row).
        For a synthesized ANSWER rather than rows, use ask_channel.

        Prerequisites: none for scope='all'; a channel_id (from list_channels)
        for scope='channel:<id>'.

        Returns (instant for one channel, longer when fanning across many;
        read-only): ``{hits: [{fact_id, text, score, channel_id, cluster_id,
        entity_tags}, ...], query: <echo of query>}`` ranked by hybrid score.
        No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'invalid_parameter' (empty/over-4000-char query, or scope not 'all'/
        'channel:<id>'); 'channel_access_denied' (only for an explicit
        'channel:<id>' the token cannot reach — under scope='all' unreachable
        channels are silently skipped).
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        if not query or len(query) > 4000:
            return {
                "error": "invalid_parameter",
                "parameter": "query",
                "detail": "length must be 1..4000 characters",
            }

        # Clamp limit so a misbehaving caller cannot drain the whole
        # corpus through one call.
        limit = max(1, min(limit, 50))

        # Resolve scope → list of channel_ids to search.
        target_channels: list[str] = []
        if scope == "all":
            try:
                from beever_atlas.capabilities import connections as conn_cap
                from beever_atlas.stores import get_stores

                stores = get_stores()
                connections = await stores.platform.list_connections()
                visible_ids: set[str] = set()
                # Mirror ``connections.list_connections`` ownership semantics
                # so search_memory does not accidentally peek at other-tenant
                # channels: re-use the public capability rather than
                # re-implementing the rule.
                visible_conns = await conn_cap.list_connections(principal_id)
                visible_conn_ids = {row["connection_id"] for row in visible_conns}
                for conn in connections:
                    if conn.id not in visible_conn_ids:
                        continue
                    for cid in conn.selected_channels or []:
                        visible_ids.add(cid)
                target_channels = sorted(visible_ids)
            except Exception:
                logger.exception(
                    "search_memory: failed to enumerate channels for principal=%s",
                    principal_id,
                )
                return {"hits": [], "query": query}
        elif scope.startswith("channel:"):
            target_channels = [scope.split(":", 1)[1]]
        else:
            return {
                "error": "invalid_parameter",
                "parameter": "scope",
                "detail": "must be 'all' or 'channel:<id>'",
            }

        if not target_channels:
            return {"hits": [], "query": query}

        # Per-channel auth + hybrid search. We collect raw hits across
        # channels then re-rank by score before truncating to ``limit``.
        from beever_atlas.capabilities import memory as mem_cap
        from beever_atlas.capabilities.errors import ChannelAccessDenied

        explicit_scope = scope.startswith("channel:")
        merged: list[dict] = []
        per_channel_limit = max(5, limit)
        for cid in target_channels:
            try:
                facts = await mem_cap.search_channel_facts(
                    principal_id,
                    cid,
                    query,
                    time_scope="any",
                    limit=per_channel_limit,
                )
            except ChannelAccessDenied:
                if explicit_scope:
                    return {
                        "error": "channel_access_denied",
                        "channel_id": cid,
                    }
                continue
            except Exception:
                logger.warning("search_memory: per-channel search failed channel=%s", cid)
                continue
            for fact in facts:
                merged.append(
                    {
                        "fact_id": fact.get("fact_id") or fact.get("id"),
                        "text": fact.get("text") or fact.get("memory_text", ""),
                        "score": float(fact.get("score") or fact.get("confidence") or 0.0),
                        "channel_id": cid,
                        "cluster_id": fact.get("cluster_id"),
                        "entity_tags": list(fact.get("entity_tags") or []),
                    }
                )

        merged.sort(key=lambda h: h["score"], reverse=True)
        return {"hits": merged[:limit], "query": query}

    # -- lint_wiki ----------------------------------------------------------
    @mcp.tool(name="lint_wiki")
    async def lint_wiki(
        channel_id: Annotated[
            str,
            "Channel id whose wiki to lint. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        ctx: Context,
        target_lang: Annotated[
            str | None,
            "Optional BCP-47 language tag to lint (e.g. 'en'). Omit to lint the "
            "channel's primary language. Default null (treated as 'en').",
        ] = None,
        run_coherence_check: Annotated[
            bool,
            "If true, also run the LLM coherence pass (one model call per page — "
            "slower and incurs token cost). Set false for a fast structural-only "
            "lint. Default true.",
        ] = True,
    ) -> dict:
        """Audit a channel's wiki for health problems and return a list of findings.
        Call it to check whether wiki pages are stale, orphaned, duplicated, or
        internally inconsistent before relying on them or recommending a refresh.

        When to use: validating wiki quality, or diagnosing why an answer looked
        wrong. When NOT to use: routine reading (use read_wiki_page /
        list_wiki_pages) — linting is heavier. Set run_coherence_check=false to
        avoid the per-page LLM cost when you only need structural checks.

        Prerequisites: a channel_id from list_channels.

        Returns (long-running when run_coherence_check=true — one LLM call per
        page; read-only, writes nothing): ``{findings: [{severity, category,
        page_id, section_id, message, suggested_action}, ...], pages_scanned: N}``.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id);
        'lint_failed' (returned as ``{findings: [], error: 'lint_failed'}`` on an
        internal lint error).
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.infra.channel_access import assert_channel_access
            from beever_atlas.services.wiki_lint import lint_channel_wiki
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            await assert_channel_access(principal_id, channel_id)

            stores = get_stores()
            page_store = WikiPageStore(db=stores.mongodb.db)
            # Live cluster ids — orphan detection compares against the
            # channel's current TopicCluster set in Weaviate. Mirrors the
            # setup in api/wiki.py::lint_wiki so the MCP and HTTP paths
            # produce identical findings.
            live_cluster_ids: set[str] = set()
            try:
                clusters = await stores.weaviate.list_clusters(channel_id)
                live_cluster_ids = {str(getattr(c, "id", "") or "") for c in clusters}
            except Exception:  # noqa: BLE001
                logger.warning(
                    "lint_wiki MCP: live-cluster enumeration failed channel=%s — "
                    "orphan detection will skip",
                    channel_id,
                )

            report = await lint_channel_wiki(
                channel_id=channel_id,
                page_store=page_store,
                target_lang=target_lang or "en",
                live_cluster_ids=live_cluster_ids,
                run_coherence_check=run_coherence_check,
                llm_provider=None,
            )
            return report.model_dump(mode="json")
        except Exception as exc:
            # ``assert_channel_access`` raises a ``HTTPException(403)``
            # under-the-hood; surface that as channel_access_denied. Other
            # exceptions are swallowed with a structured log.
            from fastapi import HTTPException

            if isinstance(exc, HTTPException) and exc.status_code in (401, 403):
                return {"error": "channel_access_denied", "channel_id": channel_id}
            logger.exception(
                "lint_wiki: failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"findings": [], "error": "lint_failed"}

    # -- get_extraction_status ---------------------------------------------
    @mcp.tool(name="get_extraction_status")
    async def get_extraction_status(
        channel_id: Annotated[
            str,
            "Channel id whose extraction progress to report. Get it from "
            "list_channels (e.g. 'ch-eng'). Required.",
        ],
        ctx: Context,
    ) -> dict:
        """Report how far fact EXTRACTION has progressed for a channel's messages,
        as a count per status. Call it to judge whether a channel's knowledge is
        fully ingested before you trust retrieval results, or to track progress
        after triggering a sync.

        Distinguish from get_job_status: this counts MESSAGES by extraction state
        (corpus readiness); get_job_status reports the lifecycle of one async JOB
        by job_id. Use this for "is this channel done extracting?"; use
        get_job_status for "did my trigger_sync/refresh_wiki job finish?".

        When to use: gauge corpus completeness, or detect a backlog (high
        ``pending``) or failures (non-zero ``failed``) before relying on
        ask_channel/search_channel_facts.

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): ``{channel_id, counts: {pending,
        extracting, done, failed}, total}`` where each count is the number of
        messages in that state and ``total`` is their sum. No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id);
        'extraction_status_failed' (internal error reading the queue).
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}

        err = _validate_id(channel_id, "channel_id")
        if err:
            return err

        try:
            from beever_atlas.infra.channel_access import assert_channel_access
            from beever_atlas.stores import get_stores

            await assert_channel_access(principal_id, channel_id)
            stores = get_stores()
            counts = await stores.mongodb.count_channel_messages_by_status(channel_id)
            total = sum(counts.values())
            return {
                "channel_id": channel_id,
                "counts": counts,
                "total": total,
            }
        except Exception as exc:
            from fastapi import HTTPException

            if isinstance(exc, HTTPException) and exc.status_code in (401, 403):
                return {"error": "channel_access_denied", "channel_id": channel_id}
            logger.exception(
                "get_extraction_status: failed principal=%s channel_id=%s",
                principal_id,
                channel_id,
            )
            return {"error": "extraction_status_failed"}

    # ----------------------------------------------------------------------
    # wiki-llm-native-redesign §7 — read-only MCP surface for the wiki
    # ----------------------------------------------------------------------
    # The legacy ``get_wiki_page(channel_id, page_type)`` tool consumes the
    # static-page namespace; these three tools expose the redesigned wiki:
    # slug-keyed identity, kind-aware filtering, and the cross-link graph.
    # All three return structured dicts; callers route through the channel
    # ACL enforced by ``assert_channel_access`` so an MCP token without
    # access to a channel sees ``{"error": "channel_access_denied"}``.
    #
    # v2 SEAM (skip-in-v1): a future change will add
    # ``propose_wiki_edit(channel_id, slug, content_md, citations)`` writing
    # to the ``wiki_proposed_edits`` Mongo collection (TTL=30d, indexed by
    # (channel_id, slug, status)). v1 ships read-only — the operator UI
    # has no surface for sandboxed agent edits yet, and the access-control
    # design needs more time. The collection is reserved at startup so
    # the v2 ship is purely additive.

    @mcp.tool(name="read_wiki_page")
    async def read_wiki_page(
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        slug: Annotated[
            str,
            "Page slug — the stable identifier of the page (e.g. "
            "'auth-architecture'). Discover valid slugs with list_wiki_pages. "
            "Required.",
        ],
        ctx: Context,
        target_lang: Annotated[
            str,
            "BCP-47 language tag for the rendered page (e.g. 'en', 'fr'). Default 'en'.",
        ] = "en",
    ) -> dict:
        """Read ONE full wiki page by its slug from the redesigned slug-keyed wiki.
        Call it after list_wiki_pages tells you which slug you want, to get a
        page's complete content and structured payload.

        Disambiguation: this is the slug-keyed redesign surface (arbitrary
        topic/entity pages). For the seven LEGACY fixed pages (overview, faq,
        decisions, ...) use get_wiki_page(page_type). Typical sequence:
        list_wiki_pages -> read_wiki_page(slug). To save tokens when you need
        only a slice, use read_wiki_module (one module) or read_wiki_section
        (one narrative section) instead of the whole page.

        Prerequisites: a channel_id from list_channels and a slug from
        list_wiki_pages.

        Returns (instant, read-only): the full WikiPage document including
        ``content_md`` (markdown body), ``kind`` + ``kind_schema`` (structured
        payload agents can iterate without re-parsing markdown), ``cross_links``
        (title->slug), ``cross_links_broken`` (linked titles with no page yet),
        ``pin_state``, and ``last_updated``. Hidden pages are excluded unless the
        token carries the ``read:hidden_pages`` scope. No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id);
        'wiki_page_not_found' (no such slug, or it is hidden and the token lacks
        read:hidden_pages); 'wiki_read_failed' (internal error).
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}
        err = _validate_id(channel_id, "channel_id") or _validate_id(slug, "slug")
        if err:
            return err
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception:
            return {"error": "channel_access_denied", "channel_id": channel_id}

        try:
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            stores = get_stores()
            page_store = WikiPageStore(db=stores.mongodb.db)
            page = await page_store.get_page_by_slug(channel_id, slug, target_lang=target_lang)
            if page is None:
                return {"error": "wiki_page_not_found", "slug": slug}
            # Honour pin_state.hidden — hidden pages drop unless the
            # caller has the ``read:hidden_pages`` scope.
            scopes = _principal_scopes(ctx)
            if (
                isinstance(page.pin_state, dict)
                and page.pin_state.get("hidden")
                and "read:hidden_pages" not in scopes
            ):
                return {"error": "wiki_page_not_found", "slug": slug}
            return page.model_dump(mode="json")
        except Exception:
            logger.exception(
                "read_wiki_page: failed principal=%s channel=%s slug=%s",
                principal_id,
                channel_id,
                slug,
            )
            return {"error": "wiki_read_failed", "slug": slug}

    @mcp.tool(name="list_wiki_pages")
    async def list_wiki_pages(
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        ctx: Context,
        kind: Annotated[
            str | None,
            "Optional kind filter. One of: 'topic', 'entity', 'decisions', 'faq', "
            "'action_items'. Omit for all kinds. Default null.",
        ] = None,
        scope: Annotated[
            str,
            "Visibility scope. 'human' (default) excludes hidden + merged pages; "
            "'all' returns everything but requires the read:hidden_pages token "
            "scope (otherwise it silently downgrades to 'human').",
        ] = "human",
        target_lang: Annotated[
            str,
            "BCP-47 language tag (e.g. 'en', 'fr'). Default 'en'.",
        ] = "en",
    ) -> dict:
        """List the wiki pages in one channel as lightweight summaries (no page
        bodies). The RECOMMENDED first call when exploring the redesigned
        slug-keyed wiki: use it to discover slugs, then fetch a page with
        read_wiki_page(slug=...).

        When to use: browsing or discovering which pages exist, or finding a slug.
        When NOT to use: you already know the slug and want the body (call
        read_wiki_page directly).

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): ``{channel_id, target_lang, scope, pages:
        [{slug, title, kind, version, last_updated, pinned, hidden}, ...]}``. The
        ``content_md`` body is intentionally omitted to keep the payload bounded
        — follow up with read_wiki_page(slug=...) for a page's content. No side
        effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id);
        'wiki_list_failed' (internal error).
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}
        err = _validate_id(channel_id, "channel_id")
        if err:
            return err
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception:
            return {"error": "channel_access_denied", "channel_id": channel_id}

        scopes = _principal_scopes(ctx)
        effective_scope = scope
        if scope == "all" and "read:hidden_pages" not in scopes:
            effective_scope = "human"

        try:
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            stores = get_stores()
            page_store = WikiPageStore(db=stores.mongodb.db)
            pages = await page_store.list_pages_by_kind(
                channel_id,
                kind=kind,
                target_lang=target_lang,
                scope=effective_scope,
            )
            return {
                "channel_id": channel_id,
                "target_lang": target_lang,
                "scope": effective_scope,
                "pages": [
                    {
                        "slug": p.slug or p.page_id.replace(":", "-"),
                        "title": p.title,
                        "kind": p.kind,
                        "version": p.version,
                        "last_updated": p.updated_at.isoformat() if p.updated_at else "",
                        "pinned": bool((p.pin_state or {}).get("pinned")),
                        "hidden": bool((p.pin_state or {}).get("hidden")),
                    }
                    for p in pages
                ],
            }
        except Exception:
            logger.exception(
                "list_wiki_pages: failed principal=%s channel=%s",
                principal_id,
                channel_id,
            )
            return {"error": "wiki_list_failed"}

    @mcp.tool(name="get_wiki_graph")
    async def get_wiki_graph(
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        ctx: Context,
    ) -> dict:
        """Return the map of how a channel's WIKI PAGES link to one another, as a
        node/edge graph. Call it to understand the wiki's structure — which pages
        reference which — or to plan a traversal across related pages.

        Disambiguation: this is the WIKI PAGE-LINK graph (nodes are wiki pages,
        edges are cross-links between them). For the KNOWLEDGE graph of entities
        and their relationships (people, systems, concepts), use
        search_relationships instead.

        When to use: visualizing or navigating wiki page structure, or finding
        clusters of related pages. When NOT to use: reading a page's content (use
        read_wiki_page) or querying entity relationships (search_relationships).

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): Cytoscape-format
        ``{channel_id, nodes: [{data: {id, label, kind, page_kind?, version?,
        last_updated?}}], edges: [{data: {id, source, target, kind}}]}``. Returns
        empty ``nodes``/``edges`` arrays (not an error) when the graph backend is
        unavailable, so it is always safe to call. No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id).
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}
        err = _validate_id(channel_id, "channel_id")
        if err:
            return err
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception:
            return {"error": "channel_access_denied", "channel_id": channel_id}
        try:
            from beever_atlas.stores import get_stores

            stores = get_stores()
            graph = stores.graph
            if not hasattr(graph, "get_wiki_graph"):
                return {"channel_id": channel_id, "nodes": [], "edges": []}
            payload = await graph.get_wiki_graph(channel_id)
            payload.setdefault("channel_id", channel_id)
            payload.setdefault("nodes", [])
            payload.setdefault("edges", [])
            return payload
        except Exception:
            logger.exception(
                "get_wiki_graph: failed principal=%s channel=%s",
                principal_id,
                channel_id,
            )
            return {"channel_id": channel_id, "nodes": [], "edges": []}

    # ----------------------------------------------------------------------
    # Round 6 — targeted LLM-agent retrieval (per-module + cross-page facts)
    # ----------------------------------------------------------------------
    # ``read_wiki_page`` returns the entire page document. For agents that
    # only need a slice (e.g. one module's structured data, all decisions
    # made by a particular author, or the source message backing a fact),
    # downloading the whole page wastes tokens. The five tools below offer
    # targeted handles:
    #   - ``read_wiki_module`` — one module's data payload
    #   - ``find_decisions``  — cross-page decision query
    #   - ``get_tensions``    — cross-page tension query (forward-compat)
    #   - ``find_facts``      — text-search facts with type filter
    #   - ``read_provenance`` — original source message for one fact
    # All five are read-only and apply the same channel ACL gate as
    # ``read_wiki_page``.

    @mcp.tool(name="read_wiki_module")
    async def read_wiki_module(
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        page_slug: Annotated[
            str,
            "Slug of the page hosting the module (e.g. 'auth-architecture'). "
            "Discover slugs via list_wiki_pages. Required.",
        ],
        anchor: Annotated[
            str,
            "Module anchor — the stable in-page id of one structured module "
            "(e.g. 'key-facts', 'decision-banner', 'tension-callout'). Discover "
            "the available anchors by reading the page once with read_wiki_page. "
            "Required.",
        ],
        ctx: Context,
        target_lang: Annotated[
            str,
            "BCP-47 language tag (e.g. 'en', 'fr'). Default 'en'.",
        ] = "en",
    ) -> dict:
        """Fetch ONE structured module from a wiki page without downloading the
        whole page. Call it when you already know the page slug and the module
        anchor you want, and reading the full page via read_wiki_page would waste
        tokens.

        When to use: you need just one module's structured data (e.g. the
        key_facts items, or a decision_banner's rationale + alternatives). When
        NOT to use: you need a narrative prose section (use read_wiki_section) or
        the whole page (use read_wiki_page). To learn a page's module anchors,
        read it once with read_wiki_page first.

        Prerequisites: a channel_id (list_channels), a page_slug
        (list_wiki_pages), and an anchor (from the page's modules).

        Returns (instant, read-only): ``{channel_id, page_slug, anchor,
        module_id, data}`` where ``data`` is the module's structured payload.
        No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id);
        'wiki_page_not_found' (no such slug); 'module_not_found' (page exists but
        has no module with that anchor); 'wiki_module_read_failed' (internal
        error).
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}
        err = (
            _validate_id(channel_id, "channel_id")
            or _validate_id(page_slug, "page_slug")
            or _validate_id(anchor, "anchor")
        )
        if err:
            return err
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception:
            return {"error": "channel_access_denied", "channel_id": channel_id}

        try:
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            stores = get_stores()
            page_store = WikiPageStore(db=stores.mongodb.db)
            page = await page_store.get_page_by_slug(channel_id, page_slug, target_lang=target_lang)
            if page is None:
                return {"error": "wiki_page_not_found", "slug": page_slug}
            for module in page.modules or []:
                if not isinstance(module, dict):
                    continue
                if module.get("anchor") == anchor:
                    return {
                        "channel_id": channel_id,
                        "page_slug": page_slug,
                        "anchor": anchor,
                        "module_id": module.get("id", ""),
                        "data": module.get("data") or {},
                    }
            return {"error": "module_not_found", "slug": page_slug, "anchor": anchor}
        except Exception:
            logger.exception(
                "read_wiki_module: failed principal=%s channel=%s slug=%s anchor=%s",
                principal_id,
                channel_id,
                page_slug,
                anchor,
            )
            return {"error": "wiki_module_read_failed"}

    @mcp.tool(name="find_decisions")
    async def find_decisions(
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        ctx: Context,
        since: Annotated[
            str | None,
            "Optional ISO-8601 date prefix (e.g. '2026-04-01'); keeps only "
            "decisions on or after this date. Omit for all dates. Default null.",
        ] = None,
        author: Annotated[
            str | None,
            "Optional exact-match author name (e.g. 'Alice Chen'). Omit for any "
            "author. Default null.",
        ] = None,
        limit: Annotated[
            int,
            "Max decisions to return, 1-100 (out-of-range values are clamped). Default 50.",
        ] = 50,
    ) -> list[dict]:
        """List the DECISIONS recorded in one channel, each with its rationale and
        rejected alternatives. Call it to answer "what did the team decide and
        why" when you want the structured decision record, not free-text facts.
        Returns a bare LIST and collapses missing-auth, access-denied, and
        internal errors into an EMPTY LIST — so ``[]`` means either no decisions
        OR no access; it never returns a structured error object and never raises.

        Disambiguation among the decision tools: use find_decisions for the
        current decision RECORDS in one channel (with rationale +
        alternatives_rejected). Use trace_decision_history to follow how one
        decision SUPERSEDED earlier ones over time (a graph timeline). Prefer
        find_decisions over find_facts(fact_type='decision') because only this
        tool enriches each result with rationale and alternatives.

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): a LIST (not a dict) of decisions sorted by
        ``decided_at`` descending, each ``{fact_id, decision (first sentence),
        decided_by, decided_at (YYYY-MM-DD), rationale (null if not yet
        extracted), alternatives_rejected, page_slug (empty if no host page yet)}``.
        No side effects.

        Error handling: on missing auth, access denial, or internal error this
        tool returns an EMPTY LIST ``[]`` rather than an error object (it never
        raises). An empty list therefore means either no decisions or no access —
        confirm access with list_channels if unexpected.
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return []
        err = _validate_id(channel_id, "channel_id")
        if err:
            return []
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception:
            return []

        # Clamp limit so a misbehaving caller cannot drain the entire
        # channel's decision history through one call. None-check rather
        # than ``or`` so ``limit=0`` clamps up to 1 instead of falling
        # back to the default.
        raw_limit = 50 if limit is None else int(limit)
        limit = max(1, min(raw_limit, 100))

        try:
            from beever_atlas.models import MemoryFilters
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            stores = get_stores()

            # 1) Pull decision-typed facts from the Weaviate store.
            #    ``list_facts`` does NOT support fact_type filtering today,
            #    so we over-fetch and filter in-memory. With limit ≤ 100
            #    this is bounded.
            facts_page = await stores.weaviate.list_facts(
                channel_id=channel_id,
                filters=MemoryFilters(),
                page=1,
                limit=max(limit * 4, 200),  # over-fetch headroom for the type filter
            )
            decisions = [f for f in facts_page.memories if f.fact_type == "decision"]

            # 2) Apply optional ``since`` / ``author`` filters.
            if since:
                decisions = [f for f in decisions if (f.message_ts or "") >= since]
            if author:
                decisions = [f for f in decisions if f.author_name == author]

            # 3) Sort by message_ts DESC, truncate to limit.
            decisions.sort(key=lambda f: f.message_ts or "", reverse=True)
            decisions = decisions[:limit]

            # 4) Build a fact_id → page_slug index from the wiki pages so
            #    each decision points back to its host page. Built once
            #    per call rather than per-fact so this stays cheap on
            #    larger channels.
            page_store = WikiPageStore(db=stores.mongodb.db)
            try:
                pages = await page_store.list_pages(channel_id, target_lang="en")
            except Exception:
                pages = []
            fact_to_slug: dict[str, str] = {}
            for page in pages:
                slug = page.slug or ""
                if not slug:
                    continue
                for fid in page.last_facts_seen or []:
                    fact_to_slug.setdefault(fid, slug)

            results: list[dict] = []
            for fact in decisions:
                memory_text = fact.memory_text or ""
                first_sentence = memory_text.split(".", 1)[0].strip()
                decided_at = ""
                if fact.message_ts:
                    decided_at = str(fact.message_ts)[:10]
                results.append(
                    {
                        "fact_id": fact.id,
                        "decision": first_sentence or memory_text,
                        "decided_by": fact.author_name or "",
                        "decided_at": decided_at,
                        "rationale": fact.rationale,
                        "alternatives_rejected": list(fact.alternatives_considered or []),
                        "page_slug": fact_to_slug.get(fact.id, ""),
                    }
                )
            return results
        except Exception:
            logger.exception(
                "find_decisions: failed principal=%s channel=%s",
                principal_id,
                channel_id,
            )
            return []

    @mcp.tool(name="get_tensions")
    async def get_tensions(
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        ctx: Context,
        status: Annotated[
            str | None,
            "Optional status filter. One of: 'open', 'blocked', 'deferred' "
            "(e.g. 'open'). Pass null (the default) to return tensions of ALL "
            "statuses; omit for the same effect.",
        ] = None,
    ) -> list[dict]:
        """List unresolved TENSIONS in one channel — points of open disagreement or
        conflicting positions surfaced across its wiki. Call it to find what is
        still contested or undecided, as opposed to settled decisions
        (find_decisions).

        When to use: surfacing open conflicts, blockers, or competing stances.
        When NOT to use: settled decisions (find_decisions) or general fact
        lookup (find_facts).

        Prerequisites: a channel_id from list_channels.

        Note: tension detection is currently empty for most channels — the wiring
        is in place but few channels have tension data yet, so an empty result is
        normal and does not indicate an error. The same call returns real data
        automatically once tensions exist, with no signature change.

        Returns (instant, read-only): a LIST (not a dict) of ``{tension_id, title,
        status, since (YYYY-MM-DD), positions: [{author, stance, fact_id}],
        page_slug}``. No side effects.

        Error handling: on missing auth, access denial, or internal error this
        tool returns an EMPTY LIST ``[]`` (it never raises) — indistinguishable
        from "no tensions"; confirm access with list_channels if unexpected.
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return []
        err = _validate_id(channel_id, "channel_id")
        if err:
            return []
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception:
            return []

        try:
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            stores = get_stores()
            page_store = WikiPageStore(db=stores.mongodb.db)
            pages = await page_store.list_pages(channel_id, target_lang="en")

            tensions: list[dict] = []
            for page in pages:
                slug = page.slug or ""
                for module in page.modules or []:
                    if not isinstance(module, dict):
                        continue
                    if module.get("id") != "tension_callout":
                        continue
                    data = module.get("data") or {}
                    if not isinstance(data, dict):
                        continue
                    # ``data`` may carry a single tension or a ``tensions``
                    # list — accept both shapes so the same wire format
                    # works whichever the renderer settles on.
                    raw_items = data.get("tensions")
                    if isinstance(raw_items, list):
                        items = raw_items
                    else:
                        items = [data]
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        tension_status = item.get("status") or "open"
                        if status and tension_status != status:
                            continue
                        since = item.get("since") or ""
                        if since and len(since) > 10:
                            since = since[:10]
                        tensions.append(
                            {
                                "tension_id": item.get("tension_id") or item.get("id") or "",
                                "title": item.get("title") or item.get("summary") or "",
                                "status": tension_status,
                                "since": since,
                                "positions": list(item.get("positions") or []),
                                "page_slug": slug,
                            }
                        )
            return tensions
        except Exception:
            logger.exception(
                "get_tensions: failed principal=%s channel=%s",
                principal_id,
                channel_id,
            )
            return []

    @mcp.tool(name="find_facts")
    async def find_facts(
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        query: Annotated[
            str,
            "Case-insensitive substring matched literally inside each fact's text "
            "(e.g. 'rollback'). NOT semantic — exact substring only. Required.",
        ],
        ctx: Context,
        fact_type: Annotated[
            str | None,
            "Optional type filter. One of: 'decision', 'observation', 'opinion', "
            "'question', 'action_item'. Omit for all types. Default null.",
        ] = None,
        limit: Annotated[
            int,
            "Max facts to return, 1-100 (out-of-range values are clamped). Default 20.",
        ] = 20,
    ) -> list[dict]:
        """Find facts in one channel whose text literally CONTAINS a substring
        (deterministic, case-insensitive). Call it when you know an exact keyword
        and want every matching raw fact row, not a ranked or synthesized result.
        Returns a bare LIST and collapses missing-auth, access-denied,
        empty-query, and internal errors into an EMPTY LIST — so ``[]`` means no
        match OR no access; it never returns a structured error object.

        Disambiguation: find_facts is a deterministic substring filter (predictable,
        no relevance ranking). For meaning-based / fuzzy retrieval use
        search_channel_facts (BM25+vector). For a synthesized answer use
        ask_channel. For decisions with rationale use find_decisions.

        When to use: exact-keyword scans ("every fact mentioning 'rollback'"),
        optionally narrowed by fact_type. When NOT to use: you want semantically
        related results for a phrase (use search_channel_facts).

        Prerequisites: a channel_id from list_channels.

        Returns (instant, read-only): a LIST (not a dict) of up to ``limit`` facts,
        sorted by importance DESC then recency (message_ts) DESC. The importance
        values that drive the sort rank, highest first, are 'critical' > 'high' >
        'medium' > 'low' (any other/empty value sorts lowest). Each item is
        ``{fact_id, memory_text, fact_type, importance, author_name, message_ts,
        page_slug (empty if not yet on a page)}``. No side effects.

        Error handling: on missing auth, access denial, empty query, or internal
        error this tool returns an EMPTY LIST ``[]`` (it never raises) — an empty
        list means no match OR no access; confirm with list_channels if
        unexpected.
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return []
        err = _validate_id(channel_id, "channel_id")
        if err:
            return []
        if not query:
            return []
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception:
            return []

        # Clamp limit (documented 1–100). Use a None-check rather than
        # ``or`` so an explicit ``limit=0`` clamps up to 1 instead of
        # silently inheriting the default 20.
        raw_limit = 20 if limit is None else int(limit)
        limit = max(1, min(raw_limit, 100))
        needle = query.lower()
        importance_rank = {"critical": 3, "high": 2, "medium": 1, "low": 0}

        try:
            from beever_atlas.models import MemoryFilters
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            stores = get_stores()
            facts_page = await stores.weaviate.list_facts(
                channel_id=channel_id,
                filters=MemoryFilters(),
                page=1,
                # Over-fetch so the in-memory substring filter has enough
                # candidates after the optional fact_type prune. Bounded
                # at ~500 so a misbehaving caller cannot stream the entire
                # channel through one tool call.
                limit=min(500, max(limit * 5, 100)),
            )
            matches = []
            for fact in facts_page.memories:
                if fact_type and fact.fact_type != fact_type:
                    continue
                if needle not in (fact.memory_text or "").lower():
                    continue
                matches.append(fact)

            # Sort: importance DESC then message_ts DESC.
            matches.sort(
                key=lambda f: (
                    importance_rank.get((f.importance or "").lower(), 0),
                    f.message_ts or "",
                ),
                reverse=True,
            )
            matches = matches[:limit]

            page_store = WikiPageStore(db=stores.mongodb.db)
            try:
                pages = await page_store.list_pages(channel_id, target_lang="en")
            except Exception:
                pages = []
            fact_to_slug: dict[str, str] = {}
            for page in pages:
                slug = page.slug or ""
                if not slug:
                    continue
                for fid in page.last_facts_seen or []:
                    fact_to_slug.setdefault(fid, slug)

            return [
                {
                    "fact_id": fact.id,
                    "memory_text": fact.memory_text or "",
                    "fact_type": fact.fact_type or "",
                    "importance": fact.importance or "",
                    "author_name": fact.author_name or "",
                    "message_ts": fact.message_ts or "",
                    "page_slug": fact_to_slug.get(fact.id, ""),
                }
                for fact in matches
            ]
        except Exception:
            logger.exception(
                "find_facts: failed principal=%s channel=%s",
                principal_id,
                channel_id,
            )
            return []

    @mcp.tool(name="read_wiki_section")
    async def read_wiki_section(
        channel_id: Annotated[
            str,
            "Channel id. Get it from list_channels (e.g. 'ch-eng'). Required.",
        ],
        page_slug: Annotated[
            str,
            "Slug of the page hosting the section (e.g. 'auth-architecture'). "
            "Discover slugs via list_wiki_pages. Required.",
        ],
        anchor: Annotated[
            str,
            "Section anchor — kebab-case in-page id of one narrative section "
            "(e.g. 'context', 'alternatives', 'implications'). If you don't know "
            "it, a 'section_not_found' error lists the available anchors. Required.",
        ],
        ctx: Context,
        target_lang: Annotated[
            str,
            "BCP-47 language tag (e.g. 'en', 'fr'). Default 'en'.",
        ] = "en",
    ) -> dict:
        """Fetch ONE narrative (prose) section of a wiki page without loading the
        whole page. Call it when you know the page slug and section anchor and
        want just that article slice, to save tokens.

        Disambiguation: read_wiki_section returns PROSE sections (paragraphs +
        citations); read_wiki_module returns a STRUCTURED module payload
        (key_facts, decision_banner, etc.); read_wiki_page returns the whole page.
        Use find_facts for fact-text search across pages.

        Prerequisites: a channel_id (list_channels), a page_slug
        (list_wiki_pages), and an anchor (from the page's narrative sections).

        Returns (instant, read-only): ``{anchor, heading, paragraphs, citations,
        visual, page_slug, page_title, channel_id}`` — ``page_title`` and
        ``channel_id`` are included so you can attribute the section without a
        second call. No side effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'channel_access_denied' (token lacks access to channel_id);
        'page_not_found' (no such slug); 'section_not_found' (page exists but lacks
        the anchor — the result lists ``available_anchors`` to retry with);
        'narrative_not_available' (page has no narrative sections — includes
        ``has_modules`` and a suggestion to use read_wiki_page for module data);
        'internal_error'.
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}
        err = (
            _validate_id(channel_id, "channel_id")
            or _validate_id(page_slug, "page_slug")
            or _validate_id(anchor, "anchor")
        )
        if err:
            return err
        try:
            from beever_atlas.infra.channel_access import assert_channel_access

            await assert_channel_access(principal_id, channel_id)
        except Exception:
            return {"error": "channel_access_denied", "channel_id": channel_id}

        try:
            from beever_atlas.stores import get_stores
            from beever_atlas.wiki.page_store import WikiPageStore

            stores = get_stores()
            page_store = WikiPageStore(db=stores.mongodb.db)
            page = await page_store.get_page_by_slug(channel_id, page_slug, target_lang=target_lang)
            if page is None:
                return {
                    "error": "page_not_found",
                    "channel_id": channel_id,
                    "page_slug": page_slug,
                }
            sections = page.narrative_sections or []
            # Page exists but predates narrative generation OR fell back —
            # surface a clear ``narrative_not_available`` so the agent
            # can fall back to read_wiki_page without retry loops.
            if not sections:
                return {
                    "error": "narrative_not_available",
                    "page_slug": page_slug,
                    "has_modules": bool(page.modules),
                    "suggestion": "Use read_wiki_page for module-only data.",
                }
            available_anchors: list[str] = []
            for section in sections:
                if not isinstance(section, dict):
                    continue
                section_anchor = str(section.get("anchor") or "")
                available_anchors.append(section_anchor)
                if section_anchor == anchor:
                    return {
                        "anchor": section_anchor,
                        "heading": section.get("heading") or "",
                        "paragraphs": list(section.get("paragraphs") or []),
                        "citations": list(section.get("citations") or []),
                        "visual": section.get("visual"),
                        "page_slug": page_slug,
                        # M-5: schema parity — agents can attribute the
                        # section without a separate read_wiki_page call.
                        "page_title": str(getattr(page, "title", "") or ""),
                        "channel_id": channel_id,
                    }
            return {
                "error": "section_not_found",
                "page_slug": page_slug,
                "available_anchors": available_anchors,
            }
        except Exception:
            logger.exception(
                "read_wiki_section: failed principal=%s channel=%s slug=%s anchor=%s",
                principal_id,
                channel_id,
                page_slug,
                anchor,
            )
            return {"error": "internal_error", "page_slug": page_slug}

    @mcp.tool(name="read_provenance")
    async def read_provenance(
        fact_id: Annotated[
            str,
            "Fact id to trace, as returned in the ``fact_id`` field of another "
            "tool's result (e.g. 'fact_abc123' from find_facts, find_decisions, "
            "search_channel_facts, or ask_channel citations). Required.",
        ],
        ctx: Context,
    ) -> dict:
        """Trace ONE fact back to the original chat message it was extracted from.
        Call it to verify or cite a fact — given a fact_id from another tool, it
        returns where the fact came from (platform, message, author, timestamp,
        and the raw message text when reachable).

        When to use: confirming a fact's source, building a citation, or auditing
        provenance. Prerequisites: a fact_id surfaced by find_facts,
        find_decisions, search_channel_facts, search_memory, or an ask_channel
        citation.

        Returns (instant, read-only): ``{fact_id, memory_text, source: {platform,
        message_id, url, author, ts}, raw_message}``. ``raw_message`` is the
        original chat body, or an empty string if the source message is no longer
        reachable — every other field is still populated in that case. No side
        effects.

        Error modes (returned as dicts): 'authentication_missing' (no principal);
        'fact_not_found' (unknown fact_id — also returned, deliberately, when the
        caller lacks access to the fact's channel, so cross-tenant existence is
        never leaked); 'provenance_read_failed' (internal error).
        """
        principal_id = _get_principal_id(ctx)
        if not principal_id:
            return {"error": "authentication_missing"}
        err = _validate_id(fact_id, "fact_id")
        if err:
            return err

        try:
            from beever_atlas.stores import get_stores

            stores = get_stores()
            fact = await stores.weaviate.get_fact(fact_id)
            if fact is None:
                return {"error": "fact_not_found", "fact_id": fact_id}

            # Best-effort channel ACL — the fact carries the channel it
            # was extracted from, so only its principal-authorized owners
            # can resolve provenance. If the auth check fails treat the
            # fact as not found to avoid leaking existence across tenants.
            try:
                from beever_atlas.infra.channel_access import assert_channel_access

                if fact.channel_id:
                    await assert_channel_access(principal_id, fact.channel_id)
            except Exception:
                return {"error": "fact_not_found", "fact_id": fact_id}

            # Pull the raw chat message body if we can find it. Missing
            # raw text is non-fatal — return the structured citation
            # block regardless so the caller still has author / ts / url.
            raw_message = ""
            if fact.source_message_id and fact.channel_id:
                try:
                    msg = await stores.mongodb.find_channel_message_by_message_id(
                        channel_id=fact.channel_id,
                        message_id=fact.source_message_id,
                    )
                    if msg:
                        raw_message = msg.get("content") or ""
                except Exception:
                    raw_message = ""

            return {
                "fact_id": fact.id,
                "memory_text": fact.memory_text or "",
                "source": {
                    "platform": fact.platform or "",
                    "message_id": fact.source_message_id or "",
                    "url": (fact.source_link_urls or [""])[0] if fact.source_link_urls else "",
                    "author": fact.author_name or "",
                    "ts": fact.message_ts or "",
                },
                "raw_message": raw_message,
            }
        except Exception:
            logger.exception(
                "read_provenance: failed principal=%s fact_id=%s",
                principal_id,
                fact_id,
            )
            return {"error": "provenance_read_failed", "fact_id": fact_id}


def _principal_scopes(ctx: Context) -> set[str]:
    """Return the MCP token scopes for the request principal.

    Wraps ``ctx`` defensively because the testing fixtures sometimes
    hand in a SimpleNamespace without the ``request_context.lifespan_context``
    chain. Empty set is the safe default — every scope-gated branch
    reads ``"foo" in scopes`` so an empty set degrades to "no extra
    scopes", which is the most-restrictive answer.
    """
    try:
        scopes = getattr(ctx, "principal_scopes", None) or getattr(
            getattr(ctx, "request_context", None), "principal_scopes", None
        )
        if scopes is None:
            return set()
        if isinstance(scopes, set | list | tuple):
            return {str(s) for s in scopes}
        return {str(scopes)}
    except Exception:
        return set()
