"""Memory-retrieval capabilities: channel-facts search, media refs, recent activity.

Framework-neutral implementations extracted from
``beever_atlas.agents.tools.memory_tools`` as part of openspec change
``atlas-mcp-server`` Phase 1. The ADK tool wrappers in
``agents/tools/memory_tools.py`` are preserved as thin shims that keep
their public ``__name__`` (locked by ``tests/test_qa_agent_name_filter_pinning.py``)
and delegate to the ``_impl`` helpers here.

Each public capability function:

* Takes ``principal_id: str`` as its first argument.
* Calls :func:`beever_atlas.infra.channel_access.assert_channel_access`
  as its first line (raises :class:`~capabilities.errors.ChannelAccessDenied`
  — a framework-neutral exception, not ``HTTPException``).
* Returns the same structured result shape the existing ADK tool returned
  so both surfaces see identical outputs.

The ``_impl`` helpers hold the actual retrieval logic without an access
check — the ADK wrappers call these because the dashboard already gates
channel access upstream in ``api/ask.py`` before the agent runs.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from beever_atlas.capabilities.errors import ChannelAccessDenied
from beever_atlas.infra.channel_access import assert_channel_access

logger = logging.getLogger(__name__)


async def _embed_query(text: str) -> list[float]:
    """Compute a Jina embedding for a query string.

    Reuses the same API path as EntityRegistry.compute_name_embedding so we
    have a single embedding code path for agent tools.  Raises on HTTP error —
    callers should catch and fall back if needed.
    """
    import httpx

    from beever_atlas.infra.config import get_settings

    settings = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            settings.jina_api_url,
            headers={
                "Authorization": f"Bearer {settings.jina_api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": settings.jina_model,
                "input": [text],
                "dimensions": settings.jina_dimensions,
                "task": "text-matching",
            },
        )
        resp.raise_for_status()
        return resp.json()["data"][0]["embedding"]


def _format_timestamp(ts: str | None) -> str:
    """Convert Slack epoch timestamp to ISO date string."""
    if not ts:
        return "(unavailable)"
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return "(unavailable)"


def _mmr_rerank(
    candidates: list[dict],
    query_tokens: set[str],
    k: int,
    lam: float = 0.6,
) -> list[dict]:
    """Lightweight MMR re-rank using token-overlap (Jaccard) similarity.

    Selects up to *k* items from *candidates* by balancing relevance to the
    query against diversity among already-selected items.  λ=1 → pure
    relevance; λ=0 → pure diversity.

    Uses pre-tokenised `text` fields; no external dependencies required.
    """
    if not candidates or k <= 0:
        return candidates[:k]

    def _tokens(text: str) -> set[str]:
        return set((text or "").lower().split())

    def _jaccard(a: set[str], b: set[str]) -> float:
        if not a and not b:
            return 0.0
        return len(a & b) / len(a | b)

    token_cache: dict[int, set[str]] = {
        i: _tokens(c.get("text", "")) for i, c in enumerate(candidates)
    }
    relevance: dict[int, float] = {
        i: _jaccard(query_tokens, token_cache[i]) for i in range(len(candidates))
    }

    selected: list[int] = []
    remaining = list(range(len(candidates)))

    while remaining and len(selected) < k:
        if not selected:
            # Bootstrap: pick highest relevance
            best = max(remaining, key=lambda i: relevance[i])
        else:
            selected_tokens = [token_cache[s] for s in selected]

            def mmr_score(i: int) -> float:
                rel = relevance[i]
                max_sim = max(_jaccard(token_cache[i], st) for st in selected_tokens)
                return lam * rel - (1 - lam) * max_sim

            best = max(remaining, key=mmr_score)
        selected.append(best)
        remaining.remove(best)

    return [candidates[i] for i in selected]


_mmr_logged = False


# ---------------------------------------------------------------------------
# search_qa_history
# ---------------------------------------------------------------------------


async def _search_qa_history_impl(channel_id: str, query: str, limit: int = 5) -> list[dict]:
    """Core implementation of QA-history search (no access check)."""
    try:
        from beever_atlas.infra.config import get_settings
        from beever_atlas.stores.qa_history_store import QAHistoryStore

        settings = get_settings()
        store = QAHistoryStore(settings.weaviate_url, settings.weaviate_api_key)
        await store.startup()
        try:
            query_vector = await _embed_query(query)
        except Exception:
            logger.warning(
                "search_qa_history: embedding failed, using bm25 fallback for channel=%s",
                channel_id,
            )
            query_vector = None
        results = await store.search_qa_history(
            channel_id=channel_id, query=query, limit=limit, query_vector=query_vector
        )
        await store.shutdown()
        if settings.qa_history_negative_filter:
            results = [r for r in results if r.get("answer_kind", "answered") != "refused"]
        return results
    except Exception:
        logger.exception("search_qa_history failed for channel=%s query=%s", channel_id, query)
        return []


async def search_qa_history(
    principal_id: str, channel_id: str, query: str, limit: int = 5
) -> list[dict]:
    """Search past Q&A history for similar questions in this channel."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:  # HTTPException from assert_channel_access
        raise ChannelAccessDenied(channel_id) from exc
    return await _search_qa_history_impl(channel_id, query, limit)


# ---------------------------------------------------------------------------
# search_channel_facts
# ---------------------------------------------------------------------------


async def _search_channel_facts_impl(
    channel_id: str,
    query: str,
    time_scope: str = "any",
    limit: int = 10,
) -> list[dict]:
    """Core implementation of atomic-facts search (no access check)."""
    global _mmr_logged
    try:
        from beever_atlas.agents.tools.channel_resolver import resolve_channel_name
        from beever_atlas.stores import get_stores

        store = get_stores().weaviate
        # Over-fetch (k*3, capped at 30) then MMR-rerank down to limit.
        fetch_limit = min(limit * 3, 30)
        try:
            query_vector = await _embed_query(query)
            raw_results = await store.true_hybrid_search(
                query_text=query,
                query_vector=query_vector,
                channel_id=channel_id,
                tier="atomic",
                limit=fetch_limit,
            )
            facts = [r["fact"] for r in raw_results]
        except Exception:
            logger.warning(
                "search_channel_facts: hybrid search failed, falling back to bm25 for channel=%s",
                channel_id,
            )
            facts = await store.bm25_search(
                query=query, channel_id=channel_id, tier="atomic", limit=fetch_limit
            )

        cutoff: datetime | None = None
        if time_scope == "recent":
            cutoff = datetime.now(tz=UTC) - timedelta(days=30)

        candidates = []
        for fact in facts:
            if cutoff and fact.message_ts:
                try:
                    ts = float(fact.message_ts)
                    fact_dt = datetime.fromtimestamp(ts, tz=UTC)
                    if fact_dt < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
            candidates.append(
                {
                    "text": fact.memory_text,
                    "author": fact.author_name,
                    "author_id": fact.author_id,
                    "channel_id": fact.channel_id,
                    "channel_name": await resolve_channel_name(fact.channel_id),
                    "platform": fact.platform,
                    "message_ts": fact.message_ts,
                    "timestamp": _format_timestamp(fact.message_ts),
                    "permalink": fact.source_message_id,
                    "importance": fact.importance,
                    "confidence": round(fact.quality_score / 10.0, 2)
                    if fact.quality_score
                    else 0.5,
                    "fact_id": fact.id,
                    "topic_tags": fact.topic_tags,
                    "media_urls": fact.source_media_urls or [],
                    "media_type": fact.source_media_type or "",
                    "link_urls": fact.source_link_urls or [],
                    "link_titles": fact.source_link_titles or [],
                }
            )

        if not _mmr_logged and len(candidates) > limit:
            logger.info(
                "search_channel_facts: MMR re-rank active (fetched=%d, returning=%d, λ=0.6)",
                len(candidates),
                limit,
            )
            _mmr_logged = True

        query_tokens = set(query.lower().split())
        return _mmr_rerank(candidates, query_tokens, k=limit)
    except Exception:
        logger.exception("search_channel_facts failed for channel=%s query=%s", channel_id, query)
        return []


async def search_channel_facts(
    principal_id: str,
    channel_id: str,
    query: str,
    time_scope: str = "any",
    limit: int = 10,
) -> list[dict]:
    """BM25+vector hybrid search over atomic facts for a channel."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc
    return await _search_channel_facts_impl(channel_id, query, time_scope=time_scope, limit=limit)


# ---------------------------------------------------------------------------
# search_media_references
# ---------------------------------------------------------------------------


async def _search_media_references_impl(
    channel_id: str,
    query: str,
    media_type: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Core implementation of media search (no access check)."""
    try:
        from beever_atlas.agents.tools.channel_resolver import resolve_channel_name
        from beever_atlas.stores import get_stores

        store = get_stores().weaviate
        try:
            query_vector = await _embed_query(query)
            raw_results = await store.true_hybrid_search(
                query_text=query,
                query_vector=query_vector,
                channel_id=channel_id,
                tier="atomic",
                limit=limit * 4,
            )
            facts = [r["fact"] for r in raw_results]
        except Exception:
            logger.warning(
                "search_media_references: hybrid search failed, falling back to bm25 for channel=%s",
                channel_id,
            )
            facts = await store.bm25_search(
                query=query, channel_id=channel_id, tier="atomic", limit=limit * 4
            )

        output = []
        for fact in facts:
            has_images = bool(fact.source_media_urls)
            has_links = bool(fact.source_link_urls)
            has_pdfs = any(".pdf" in u for u in (fact.source_link_urls or []))

            if media_type == "image" and not has_images:
                continue
            if media_type == "pdf" and not has_pdfs:
                continue
            if media_type == "link" and not has_links:
                continue
            if media_type is None and not (has_images or has_links):
                continue

            output.append(
                {
                    "text": fact.memory_text,
                    "media_urls": fact.source_media_urls or [],
                    "link_urls": fact.source_link_urls or [],
                    "link_titles": fact.source_link_titles or [],
                    "author": fact.author_name,
                    "channel_id": fact.channel_id,
                    "channel_name": await resolve_channel_name(fact.channel_id)
                    if fact.channel_id
                    else "",
                    "platform": fact.platform,
                    "message_ts": fact.message_ts,
                    "timestamp": _format_timestamp(fact.message_ts),
                    "media_type": fact.source_media_type or "unknown",
                    "fact_id": fact.id,
                }
            )
            if len(output) >= limit:
                break
        return output
    except Exception:
        logger.exception("search_media_references failed for channel=%s", channel_id)
        return []


async def search_media_references(
    principal_id: str,
    channel_id: str,
    query: str,
    media_type: str | None = None,
    limit: int = 5,
) -> list[dict]:
    """Search for images / PDFs / links shared in the channel."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc
    return await _search_media_references_impl(
        channel_id, query, media_type=media_type, limit=limit
    )


# ---------------------------------------------------------------------------
# get_recent_activity
# ---------------------------------------------------------------------------


async def _get_recent_activity_impl(
    channel_id: str,
    days: int = 7,
    topic: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Core implementation of recent-activity retrieval (no access check)."""
    try:
        from beever_atlas.agents.tools.channel_resolver import resolve_channel_name
        from beever_atlas.stores import get_stores

        store = get_stores().weaviate
        search_query = topic or "recent updates"
        try:
            query_vector = await _embed_query(search_query)
            raw_results = await store.true_hybrid_search(
                query_text=search_query,
                query_vector=query_vector,
                channel_id=channel_id,
                tier="atomic",
                limit=limit * 3,
            )
            facts = [r["fact"] for r in raw_results]
        except Exception:
            logger.warning(
                "get_recent_activity: hybrid search failed, falling back to bm25 for channel=%s",
                channel_id,
            )
            facts = await store.bm25_search(
                query=search_query, channel_id=channel_id, tier="atomic", limit=limit * 3
            )

        cutoff = datetime.now(tz=UTC) - timedelta(days=days)
        output = []
        for fact in facts:
            if fact.message_ts:
                try:
                    ts = float(fact.message_ts)
                    fact_dt = datetime.fromtimestamp(ts, tz=UTC)
                    if fact_dt >= cutoff:
                        output.append(
                            {
                                "text": fact.memory_text,
                                "author": fact.author_name,
                                "author_id": fact.author_id,
                                "channel_id": fact.channel_id,
                                "channel_name": await resolve_channel_name(fact.channel_id)
                                if hasattr(fact, "channel_id") and fact.channel_id
                                else "",
                                "platform": getattr(fact, "platform", "slack"),
                                "message_ts": fact.message_ts,
                                "timestamp": _format_timestamp(fact.message_ts),
                                "importance": fact.importance,
                                "topic_tags": fact.topic_tags,
                                "fact_id": fact.id,
                            }
                        )
                except (ValueError, TypeError):
                    pass

        output.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        return output[:limit]
    except Exception:
        logger.exception("get_recent_activity failed for channel=%s", channel_id)
        return []


async def get_recent_activity(
    principal_id: str,
    channel_id: str,
    days: int = 7,
    topic: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Return recent facts from the channel, optionally filtered by topic."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc
    return await _get_recent_activity_impl(channel_id, days=days, topic=topic, limit=limit)


__all__ = [
    "search_qa_history",
    "search_channel_facts",
    "search_media_references",
    "get_recent_activity",
    "_search_qa_history_impl",
    "_search_channel_facts_impl",
    "_search_media_references_impl",
    "_get_recent_activity_impl",
]
