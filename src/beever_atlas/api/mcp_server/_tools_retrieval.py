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

    # -- search_memory ------------------------------------------------------
    @mcp.tool(name="search_memory")
    async def search_memory(
        query: Annotated[str, "Natural-language or keyword search query"],
        ctx: Context,
        scope: Annotated[
            str,
            "Either 'all' (default — search every channel the principal can access) "
            "or 'channel:<channel_id>' (search a single channel)",
        ] = "all",
        limit: Annotated[int, "Maximum hits across all channels (1–50)"] = 20,
    ) -> dict:
        """Cross-channel agent recall via Weaviate hybrid search.

        Use this when the agent does not yet know which channel holds the
        relevant memory — it merges hybrid (BM25 + vector) results from
        every channel the principal can access. Each hit carries
        ``fact_id``, ``text``, ``score``, ``channel_id``, ``cluster_id``,
        ``entity_tags``. Results are ranked by hybrid score across the
        merged set.

        scope:
          - ``"all"``: enumerate the principal's accessible channels and
            search each (auth-gated per channel). Use this when the agent
            does not yet know which channel holds the relevant memory.
          - ``"channel:<id>"``: single-channel search, equivalent to
            ``search_channel_facts`` but with the standard search_memory
            response shape.

        Returns: ``{hits: [...], query: <echo>}`` or
        ``{error: "channel_access_denied", channel_id: ...}`` for an
        explicit unreachable channel scope.
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
        channel_id: Annotated[str, "The channel id whose wiki should be linted"],
        ctx: Context,
        target_lang: Annotated[
            str | None,
            "Language tag to lint (defaults to the channel's primary)",
        ] = None,
        run_coherence_check: Annotated[
            bool,
            "Run the bounded LLM coherence pass (one call per page)",
        ] = True,
    ) -> dict:
        """Run the wiki lint checks for a channel and return findings.

        Wraps the same ``POST /api/channels/{id}/wiki/lint`` HTTP endpoint
        used by the WikiHealthToolbar UI. Surfaces orphan pages, stale
        sections, duplicate sections, and intra-page coherence issues.
        Each finding carries ``severity``, ``category``, ``page_id``,
        ``section_id``, ``message``, and ``suggested_action``.

        Returns: ``{findings: [...], pages_scanned: N}`` or
        ``{error: "channel_access_denied", ...}``.
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
        channel_id: Annotated[str, "The channel id whose extraction queue depth to return"],
        ctx: Context,
    ) -> dict:
        """Return per-status extraction counts for a channel.

        Backs operator + agent visibility into the background
        ExtractionWorker queue. Wraps the same ``GET
        /api/channels/{id}/extraction-status`` HTTP endpoint used by the
        Sync Progress + WikiTab UIs. Returns
        ``{channel_id, counts: {pending, extracting, done, failed}, total}``.
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
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        slug: Annotated[
            str,
            "The page slug — stable, human-readable identifier from the wiki",
        ],
        ctx: Context,
        target_lang: Annotated[str, "Target language tag (BCP-47); defaults to 'en'"] = "en",
    ) -> dict:
        """Return the structured payload for one wiki page (slug-keyed).

        Returns the full WikiPage document including ``content_md`` (markdown
        for human reading), ``kind`` + ``kind_schema`` (structured payload
        agents can iterate without re-parsing markdown), ``cross_links``
        (title→slug), ``cross_links_broken`` (titles with no destination
        page yet), ``pin_state``, and ``last_updated``. Hidden pages are
        excluded UNLESS the caller's MCP token carries the
        ``read:hidden_pages`` scope (set in ``BEEVER_MCP_API_KEY_SCOPES``).

        Returns ``{error: "wiki_page_not_found"}`` on missing slug,
        ``{error: "channel_access_denied"}`` on ACL denial.
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
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        ctx: Context,
        kind: Annotated[
            str | None,
            "Optional kind filter: topic / entity / decisions / faq / action_items",
        ] = None,
        scope: Annotated[
            str,
            "'human' (default) excludes hidden + merged pages; 'all' returns "
            "everything when the caller has read:hidden_pages",
        ] = "human",
        target_lang: Annotated[str, "Target language tag (BCP-47)"] = "en",
    ) -> dict:
        """Return a list of wiki pages for a channel, optionally filtered.

        Returns ``{channel_id, target_lang, scope, pages: [<summary>...]}``
        where each summary carries ``slug``, ``title``, ``kind``,
        ``last_updated``, ``version``, and ``pin_state.pinned/hidden``. The
        ``content_md`` body is intentionally NOT included — agents that
        need the body should follow up with ``read_wiki_page(slug=...)``
        to keep the per-call payload bounded.

        ``scope="all"`` requires the ``read:hidden_pages`` token scope —
        otherwise the caller silently downgrades to ``scope="human"``.
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
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        ctx: Context,
    ) -> dict:
        """Return the channel's wiki cross-link graph in Cytoscape format.

        Same payload as ``GET /api/channels/{cid}/wiki/graph``:
        ``{channel_id, nodes: [{data:{id,label,kind,page_kind?,version?,last_updated?}}],
        edges: [{data:{id,source,target,kind}}]}``. Empty arrays when
        the graph backend is unavailable so the route remains
        always-200 across deployments.
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
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        page_slug: Annotated[
            str,
            "The slug of the wiki page that hosts the module",
        ],
        anchor: Annotated[
            str,
            "The module anchor — stable in-page id, e.g. 'key-facts', "
            "'decision-banner', 'tension-callout'",
        ],
        ctx: Context,
        target_lang: Annotated[str, "Target language tag (BCP-47)"] = "en",
    ) -> dict:
        """Fetch a single module's structured payload from a wiki page.

        Returns the module's ``data`` dict (e.g. for ``key_facts``, the
        items list; for ``decision_banner``, the rationale + alternatives
        rejected). Use this when an agent only needs one slice of a page
        and reading the entire page via ``read_wiki_page`` would waste
        tokens.

        Returns ``{error: "wiki_page_not_found"}`` when the page does not
        exist, ``{error: "module_not_found"}`` when the page exists but
        does not contain the named anchor, or
        ``{error: "channel_access_denied"}`` on ACL denial.
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
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        ctx: Context,
        since: Annotated[
            str | None,
            "Optional ISO-8601 date prefix (e.g. '2026-04-01'); only "
            "decisions on or after this date are returned",
        ] = None,
        author: Annotated[
            str | None,
            "Optional exact-match author_name filter",
        ] = None,
        limit: Annotated[int, "Maximum number of decisions to return (1–100)"] = 50,
    ) -> list[dict]:
        """Find every decision recorded in a channel's wiki / fact store.

        Returns a list of decisions sorted by ``decided_at`` descending.
        Each entry includes ``fact_id``, ``decision`` (first sentence of
        the fact's memory_text), ``decided_by`` (author_name),
        ``decided_at`` (YYYY-MM-DD prefix of message_ts), ``rationale``
        (Phase 3 enrichment — null when not yet extracted),
        ``alternatives_rejected``, and ``page_slug`` (the wiki page where
        this decision lives, when known — empty string when no host page
        has integrated this decision yet).

        Filters:
        - ``since="2026-04-01"`` returns decisions whose ``message_ts``
          starts on or after that date.
        - ``author="Alice Chen"`` returns decisions with exact-match
          ``author_name``.

        Use this instead of ``find_facts(fact_type="decision")`` when you
        also need rationale / alternatives_rejected on each result.
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
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        ctx: Context,
        status: Annotated[
            str | None,
            "Optional status filter — 'open' | 'blocked' | 'deferred'",
        ] = None,
    ) -> list[dict]:
        """List unresolved tensions across the channel's wiki.

        Walks every wiki page for the channel and surfaces ``tension_callout``
        modules. Each result carries ``tension_id``, ``title``, ``status``,
        ``since`` (YYYY-MM-DD), ``positions`` (list of ``{author, stance,
        fact_id}``), and ``page_slug`` (the page where the tension lives).

        Forward-compatible: tension detection is not yet shipped on this
        track, so this tool returns ``[]`` for most channels today. The
        wiring is in place so the same call starts returning real data
        once tension detection lands without an API change.

        Filters: ``status="open"`` keeps only tensions whose status field
        equals the requested value. With no filter, every tension on
        every page is returned.
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
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        query: Annotated[str, "Case-insensitive substring to match in memory_text"],
        ctx: Context,
        fact_type: Annotated[
            str | None,
            "Optional type filter — 'decision' | 'observation' | 'opinion' | "
            "'question' | 'action_item'",
        ] = None,
        limit: Annotated[int, "Maximum number of facts to return (1–100)"] = 20,
    ) -> list[dict]:
        """Search facts by text query within a channel.

        Returns up to ``limit`` facts whose ``memory_text`` contains
        ``query`` (case-insensitive substring), optionally filtered by
        ``fact_type``. Each result carries ``fact_id``, ``memory_text``,
        ``fact_type``, ``importance``, ``author_name``, ``message_ts``,
        and ``page_slug`` (the wiki page where this fact lives, or empty
        when not yet integrated).

        Use this when ``ask_channel`` would over-synthesize and you just
        want raw fact rows that mention a keyword. For semantic / vector
        search use ``search_channel_facts`` instead — this tool is a
        deterministic substring filter, not a ranked retriever.
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
        channel_id: Annotated[str, "The channel id (from list_channels)"],
        page_slug: Annotated[
            str,
            "The slug of the wiki page that hosts the narrative section",
        ],
        anchor: Annotated[
            str,
            "The narrative section anchor — kebab-case in-page id "
            "(e.g. 'context', 'alternatives', 'implications')",
        ],
        ctx: Context,
        target_lang: Annotated[str, "Target language tag (BCP-47)"] = "en",
    ) -> dict:
        """Fetch ONE narrative section's structured data without loading
        the full page.

        Saves tokens for agents that only need a slice of a wiki article.
        Returns ``{anchor, heading, paragraphs, citations, visual,
        page_slug, page_title, channel_id}`` on hit — ``page_title`` and
        ``channel_id`` are included so the agent can render attribution
        without a separate ``read_wiki_page`` call. Use this instead of
        ``read_wiki_page`` when you know the section anchor; use
        ``read_wiki_module`` for ONE module's structured payload
        (key_facts, decision_log, etc.); use ``find_facts`` for
        fact-text search across pages.

        Returns ``{error: "page_not_found", channel_id, page_slug}`` when
        the page does not exist; ``{error: "section_not_found",
        page_slug, available_anchors: [...]}`` when the page exists but
        the anchor is missing; ``{error: "narrative_not_available",
        page_slug, has_modules: bool, suggestion: ...}`` when the page
        predates narrative generation OR fell back due to validation
        failure (callers can retry with ``read_wiki_page`` for module-
        only data); ``{error: "channel_access_denied"}`` on ACL denial.

        Spec:
        ``openspec/changes/wiki-narrative-articles/specs/mcp-redesign-tools/``.
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
        fact_id: Annotated[str, "The fact id whose source message to return"],
        ctx: Context,
    ) -> dict:
        """Fetch the original source message for a fact.

        Closes the audit loop — given a fact_id surfaced by another tool
        (``find_decisions``, ``find_facts``, ``ask_channel``...), this
        returns the platform / message_id / author / timestamp it was
        extracted from, plus the raw chat message body when reachable.

        Returns:
        ``{fact_id, memory_text, source: {platform, message_id, url,
        author, ts}, raw_message}``.

        Returns ``{error: "fact_not_found"}`` when the fact_id is unknown.
        Does NOT fail hard if the source message itself is unreachable —
        the ``raw_message`` field is empty in that case but every other
        field is still populated.
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
