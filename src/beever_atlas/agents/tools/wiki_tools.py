"""Wiki and topic overview tools for the QA agent."""

from __future__ import annotations

import logging

from beever_atlas.agents.tools._citation_decorator import cite_tool_output

logger = logging.getLogger(__name__)

SUPPORTED_PAGE_TYPES = frozenset(
    {"overview", "faq", "decisions", "people", "glossary", "activity", "topics"}
)


@cite_tool_output(kind="wiki_page")
async def get_wiki_page(channel_id: str, page_type: str) -> dict | None:
    """Retrieve a pre-compiled wiki page from MongoDB wiki_cache.

    Cost: $0. Target latency: <50ms (cache read only, no Weaviate/Neo4j queries).

    Args:
        channel_id: The channel to look up.
        page_type: One of: overview, faq, decisions, people, glossary, activity, topics.

    Returns:
        Dict with page_type, content (markdown), and summary — or None if unavailable.
    """
    if page_type not in SUPPORTED_PAGE_TYPES:
        return None
    try:
        from beever_atlas.infra.config import get_settings
        from beever_atlas.wiki.cache import WikiCache

        settings = get_settings()
        cache = WikiCache(settings.mongodb_uri)
        page = await cache.get_page(channel_id, page_type)
        if page is None:
            return None
        summary_text = page.get("summary", "")
        content_text = page.get("content", "")

        # Fix: stale "No activity recorded in the last 7 days" sentinel.
        # When the stored activity page echoes the canned empty message,
        # attempt a live fallback from get_recent_activity before returning.
        _STALE_SENTINEL = "No activity recorded in the last 7 days"
        if page_type == "activity" and _STALE_SENTINEL in (content_text or summary_text or ""):
            from beever_atlas.agents.tools.memory_tools import get_recent_activity

            fresh_facts = await get_recent_activity(channel_id, days=7, limit=5)
            if fresh_facts:
                # Format top-5 facts as a lightweight activity summary.
                lines = [
                    f"- [{f.get('timestamp', '')}] {f.get('author', 'unknown')}: {f.get('text', '')}"
                    for f in fresh_facts[:5]
                ]
                fresh_content = "\n".join(lines)
                return {
                    "page_type": page_type,
                    "channel_id": channel_id,
                    "content": fresh_content,
                    "summary": f"Recent activity ({len(fresh_facts)} items)",
                    "text": fresh_content[:400],
                }
            # Truly empty channel — return None so the agent doesn't echo the sentinel.
            return None

        # Expose fields the citation decorator reads: channel_id and a
        # text excerpt (`text`) it treats as the grounding text.
        return {
            "page_type": page_type,
            "channel_id": channel_id,
            "content": content_text,
            "summary": summary_text,
            "text": summary_text or content_text[:400],
        }
    except Exception:
        logger.exception(
            "get_wiki_page failed for channel=%s page_type=%s", channel_id, page_type
        )
        return None


@cite_tool_output(kind="wiki_page")
async def get_topic_overview(
    channel_id: str, topic_name: str | None = None
) -> dict | None:
    """Retrieve channel-level summary (Tier 0) or a topic cluster summary (Tier 1).

    Cost: $0 (cached). Target latency: <50ms.

    Args:
        channel_id: The channel to look up.
        topic_name: Optional topic to narrow to a matching Tier 1 cluster.

    Returns:
        Dict with tier, summary, and metadata — or None if unavailable.
    """
    try:
        from beever_atlas.stores import get_stores

        store = get_stores().weaviate

        if topic_name is None:
            summary = await store.get_channel_summary(channel_id)
            if summary is None:
                return None
            return {
                "tier": "summary",
                "channel_id": channel_id,
                "page_type": "overview",
                "summary": summary.text,
                "text": summary.text,
                "cluster_count": summary.cluster_count,
                "fact_count": summary.fact_count,
            }

        clusters = await store.list_clusters(channel_id)
        topic_lower = topic_name.lower()
        best = None
        for cluster in clusters:
            tags = [t.lower() for t in (cluster.topic_tags or [])]
            if any(topic_lower in t or t in topic_lower for t in tags):
                best = cluster
                break
        if best is None and clusters:
            best = clusters[0]
        if best is None:
            return None
        return {
            "tier": "topic",
            "channel_id": channel_id,
            "page_type": "topics",
            "slug": (best.topic_tags[0] if best.topic_tags else None) or topic_name,
            "cluster_id": best.id,
            "summary": best.summary,
            "text": best.summary,
            "topic_tags": best.topic_tags,
            "member_count": best.member_count,
        }
    except Exception:
        logger.exception(
            "get_topic_overview failed for channel=%s topic=%s", channel_id, topic_name
        )
        return None
