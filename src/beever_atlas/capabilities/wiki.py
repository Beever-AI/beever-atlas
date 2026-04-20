"""Wiki capabilities: get wiki pages, topic overviews, and wiki refresh.

Framework-neutral implementations for openspec change ``atlas-mcp-server``
Phase 1 (tasks 1.4 partial, 1.6). Delegates to the existing
``agents/tools/wiki_tools.py`` logic (via the ``_impl`` helpers) and to
``wiki/builder.py`` for refresh.

Each public capability function:

* Takes ``principal_id: str`` as its first argument.
* Calls :func:`beever_atlas.infra.channel_access.assert_channel_access`
  as its first line (raises :class:`~capabilities.errors.ChannelAccessDenied`).
* Returns the same structured result the existing ADK tool returned.

The ADK wrappers in ``agents/tools/wiki_tools.py`` are preserved as thin
shims that call the ``_impl`` helpers here without an access check (the
dashboard already gates channel access upstream).
"""

from __future__ import annotations

import logging

from beever_atlas.capabilities.errors import ChannelAccessDenied
from beever_atlas.infra.channel_access import assert_channel_access

logger = logging.getLogger(__name__)

SUPPORTED_PAGE_TYPES = frozenset(
    {"overview", "faq", "decisions", "people", "glossary", "activity", "topics"}
)


# ---------------------------------------------------------------------------
# _impl helpers (no access check — called by ADK wrappers and public fns)
# ---------------------------------------------------------------------------


async def _get_wiki_page_impl(
    channel_id: str,
    page_type: str = "overview",
) -> dict | None:
    """Core implementation of wiki-page retrieval (no access check)."""
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
        _STALE_SENTINEL = "No activity recorded in the last 7 days"
        if page_type == "activity" and _STALE_SENTINEL in (content_text or summary_text or ""):
            from beever_atlas.capabilities.memory import _get_recent_activity_impl

            fresh_facts = await _get_recent_activity_impl(channel_id, days=7, limit=5)
            if fresh_facts:
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
            return None

        return {
            "page_type": page_type,
            "channel_id": channel_id,
            "content": content_text,
            "summary": summary_text,
            "text": summary_text or content_text[:400],
        }
    except Exception:
        logger.exception("get_wiki_page failed for channel=%s page_type=%s", channel_id, page_type)
        return None


async def _get_topic_overview_impl(
    channel_id: str,
    topic_name: str | None = None,
) -> dict | None:
    """Core implementation of topic-overview retrieval (no access check)."""
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


# ---------------------------------------------------------------------------
# Public capability functions (with access check)
# ---------------------------------------------------------------------------


async def get_wiki_page(
    principal_id: str,
    channel_id: str,
    page_type: str = "overview",
) -> dict | None:
    """Retrieve a pre-compiled wiki page; enforces channel access."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc
    return await _get_wiki_page_impl(channel_id, page_type)


async def get_topic_overview(
    principal_id: str,
    channel_id: str,
    topic_name: str | None = None,
) -> dict | None:
    """Retrieve channel-level or topic-cluster summary; enforces channel access."""
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc
    return await _get_topic_overview_impl(channel_id, topic_name)


_WIKI_REFRESH_COOLDOWN_MINUTES = 5


async def refresh_wiki(
    principal_id: str,
    channel_id: str,
    page_types: list[str] | None = None,
) -> dict:
    """Trigger async wiki regeneration for *channel_id*.

    Enforces :func:`assert_channel_access`, enforces a ``5``-minute cooldown
    per channel so an external agent cannot queue concurrent
    LLM-heavy regeneration runs, then creates a ``sync_jobs`` record with
    ``kind="wiki_refresh"`` and dispatches generation via ``wiki/builder.py``.

    Raises :class:`~capabilities.errors.CooldownActive` when a
    ``wiki_refresh`` job for the same channel completed within the cooldown
    window. Caller (MCP tool / dashboard endpoint) translates that into the
    ``cooldown_active`` structured error.

    Returns ``{"job_id": "...", "status_uri": "atlas://job/<id>", "status": "queued"}``.
    """
    from datetime import UTC, datetime, timedelta

    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc

    from beever_atlas.capabilities.errors import CooldownActive
    from beever_atlas.infra.config import get_settings
    from beever_atlas.stores import get_stores
    from beever_atlas.wiki.cache import WikiCache

    stores = get_stores()
    settings = get_settings()

    # Cooldown check: reject if a prior wiki_refresh completed within the
    # window. A still-running job is NOT blocked here — the rate limiter
    # caps concurrency upstream, and callers may legitimately check status
    # via get_job_status while another run is in flight.
    # Filter by kind so a recent ``sync`` job does not trigger the
    # wiki-specific cooldown (Fix #4).
    try:
        recent = await stores.mongodb.get_last_job_by_kind(channel_id, "wiki_refresh")
    except Exception:
        recent = None
    if recent and recent.status in {"completed", "failed"} and recent.completed_at is not None:
        completed = recent.completed_at
        if completed.tzinfo is None:
            completed = completed.replace(tzinfo=UTC)
        elapsed = datetime.now(tz=UTC) - completed
        window = timedelta(minutes=_WIKI_REFRESH_COOLDOWN_MINUTES)
        if elapsed < window:
            remaining = window - elapsed
            raise CooldownActive(int(remaining.total_seconds()))

    cache = WikiCache(settings.mongodb_uri)

    # Create a sync_jobs record and use the *persisted* job id so the
    # atlas://job/<id> resource resolves to the real row (Phase 1 bug fix).
    job_id: str | None = None
    is_persisted_job = False
    try:
        job = await stores.mongodb.create_sync_job(
            channel_id=channel_id,
            sync_type="wiki_refresh",
            total_messages=0,
            owner_principal_id=principal_id,
            kind="wiki_refresh",
        )
        job_id = job.id
        is_persisted_job = True
    except Exception:
        logger.warning(
            "refresh_wiki: could not create sync_jobs record for channel=%s — "
            "falling back to a synthetic job_id",
            channel_id,
        )

    if job_id is None:
        import uuid as _uuid

        job_id = str(_uuid.uuid4())

    # Set status to "running" immediately so the frontend sees it on first poll.
    try:
        await cache.set_generation_status(
            channel_id,
            status="running",
            stage="starting",
            stage_detail="Initiating wiki generation…",
        )
    except Exception:
        logger.debug("refresh_wiki: set_generation_status not available, skipping")

    # Fire off background generation (best-effort; we don't await it).
    import asyncio

    from beever_atlas.wiki.builder import WikiBuilder

    async def _run() -> None:
        try:
            builder = WikiBuilder(stores.weaviate, stores.graph, cache)
            await builder.refresh_wiki(channel_id)
            if is_persisted_job:
                try:
                    await stores.mongodb.complete_sync_job(job_id, status="completed")
                except Exception:
                    logger.warning(
                        "refresh_wiki: failed to mark job completed job_id=%s",
                        job_id,
                    )
        except Exception as exc:
            logger.error("refresh_wiki: generation failed channel=%s: %s", channel_id, exc)
            if is_persisted_job:
                try:
                    await stores.mongodb.complete_sync_job(
                        job_id, status="failed", errors=[str(exc)]
                    )
                except Exception:
                    pass
            try:
                await cache.set_generation_status(
                    channel_id, status="failed", stage="error", error=str(exc)
                )
            except Exception:
                pass

    asyncio.ensure_future(_run())

    return {
        "job_id": job_id,
        "status_uri": f"atlas://job/{job_id}",
        "status": "queued",
    }


__all__ = [
    "get_wiki_page",
    "get_topic_overview",
    "refresh_wiki",
    "_get_wiki_page_impl",
    "_get_topic_overview_impl",
]
