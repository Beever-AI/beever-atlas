"""Topic cluster and channel summary API endpoints."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(tags=["topics"])


class TopicClusterResponse(BaseModel):
    id: str
    summary: str
    topic_tags: list[str]
    member_count: int
    key_entities: list[dict] = []
    key_relationships: list[dict] = []
    date_range_start: str = ""
    date_range_end: str = ""
    authors: list[str] = []
    media_refs: list[str] = []
    media_names: list[str] = []
    link_refs: list[str] = []
    high_importance_count: int = 0
    related_cluster_ids: list[str] = []
    staleness_score: float = 0.0
    status: str = "active"
    fact_type_counts: dict[str, int] = {}
    title: str = ""
    current_state: str = ""
    open_questions: str = ""
    impact_note: str = ""
    key_facts: list[dict] = []
    decisions: list[dict] = []
    people: list[dict] = []
    technologies: list[dict] = []
    projects: list[dict] = []
    faq_candidates: list[dict] = []


class TopicClusterDetailResponse(BaseModel):
    id: str
    summary: str
    topic_tags: list[str]
    member_count: int
    members: list[dict]


class ChannelSummaryResponse(BaseModel):
    text: str
    cluster_count: int
    fact_count: int
    key_decisions: list[dict] = []
    key_entities: list[dict] = []
    key_topics: list[dict] = []
    date_range_start: str = ""
    date_range_end: str = ""
    media_count: int = 0
    author_count: int = 0
    worst_staleness: float = 0.0
    channel_name: str = ""
    description: str = ""
    themes: str = ""
    momentum: str = ""
    team_dynamics: str = ""
    top_decisions: list[dict] = []
    top_people: list[dict] = []
    tech_stack: list[dict] = []
    active_projects: list[dict] = []
    glossary_terms: list[dict] = []
    recent_activity_summary: dict = {}
    topic_graph_edges: list[dict] = []


class EntityCardResponse(BaseModel):
    entity_name: str
    entity_type: str
    channel_ids: list[str] = []
    cluster_ids: list[str] = []
    fact_count: int = 0
    fact_type_breakdown: dict[str, int] = {}
    key_facts: list[str] = []
    related_entities: list[dict] = []
    last_mentioned_at: str = ""
    staleness_score: float = 0.0
    summary: str = ""


@router.get(
    "/api/channels/{channel_id}/topics",
    response_model=list[TopicClusterResponse],
)
async def list_topics(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> list[TopicClusterResponse]:
    """List all topic clusters for a channel."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    clusters = await stores.weaviate.list_clusters(channel_id)
    # Sort by member_count descending
    clusters.sort(key=lambda c: c.member_count, reverse=True)
    return [
        TopicClusterResponse(
            id=c.id,
            summary=c.summary,
            topic_tags=c.topic_tags,
            member_count=c.member_count,
            key_entities=c.key_entities,
            key_relationships=c.key_relationships,
            date_range_start=c.date_range_start,
            date_range_end=c.date_range_end,
            authors=c.authors,
            media_refs=c.media_refs,
            media_names=c.media_names,
            link_refs=c.link_refs,
            high_importance_count=c.high_importance_count,
            related_cluster_ids=c.related_cluster_ids,
            staleness_score=c.staleness_score,
            status=c.status,
            fact_type_counts=c.fact_type_counts,
            title=c.title,
            current_state=c.current_state,
            open_questions=c.open_questions,
            impact_note=c.impact_note,
            key_facts=c.key_facts,
            decisions=c.decisions,
            people=c.people,
            technologies=c.technologies,
            projects=c.projects,
            faq_candidates=c.faq_candidates,
        )
        for c in clusters
    ]


@router.get(
    "/api/channels/{channel_id}/topics/{cluster_id}",
    response_model=TopicClusterDetailResponse,
)
async def get_topic(
    channel_id: str,
    cluster_id: str,
    principal: Principal = Depends(require_user),
) -> TopicClusterDetailResponse:
    """Get a topic cluster with its member facts."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    cluster = await stores.weaviate.get_cluster(cluster_id)
    if cluster is None:
        raise HTTPException(status_code=404, detail=f"Topic cluster {cluster_id} not found")
    members = await stores.weaviate.get_cluster_members(cluster_id)
    return TopicClusterDetailResponse(
        id=cluster.id,
        summary=cluster.summary,
        topic_tags=cluster.topic_tags,
        member_count=cluster.member_count,
        members=[m.model_dump(exclude={"text_vector"}) for m in members],
    )


@router.get(
    "/api/channels/{channel_id}/summary",
    response_model=ChannelSummaryResponse,
)
async def get_channel_summary(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> ChannelSummaryResponse:
    """Get the Tier 0 channel summary."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    summary = await stores.weaviate.get_channel_summary(channel_id)
    if summary is None:
        raise HTTPException(status_code=404, detail="No channel summary available yet")
    # Backfill empty channel_name from MongoDB sync state. Older summaries
    # were written with channel_name="" because the consolidation entry
    # points defaulted it; without this fallback the UI shows the raw
    # channel_id as the heading. Final fallback is channel_id itself.
    resolved_name = summary.channel_name
    if not resolved_name:
        display = await stores.mongodb.get_channel_display_name(channel_id)
        if not display:
            # Activity-event lookup can miss on first sync; ask the adapter
            # directly as a last authoritative source before falling back
            # to the raw id.
            try:
                from beever_atlas.adapters.bridge import BridgeClient
                async with BridgeClient() as client:
                    info = await client.get_channel_info(channel_id)
                    display = info.channel_name
            except Exception:
                display = None
        resolved_name = display or channel_id
    return ChannelSummaryResponse(
        text=summary.text,
        cluster_count=summary.cluster_count,
        fact_count=summary.fact_count,
        key_decisions=summary.key_decisions,
        key_entities=summary.key_entities,
        key_topics=summary.key_topics,
        date_range_start=summary.date_range_start,
        date_range_end=summary.date_range_end,
        media_count=summary.media_count,
        author_count=summary.author_count,
        worst_staleness=summary.worst_staleness,
        channel_name=resolved_name,
        description=summary.description,
        themes=summary.themes,
        momentum=summary.momentum,
        team_dynamics=summary.team_dynamics,
        top_decisions=summary.top_decisions,
        top_people=summary.top_people,
        tech_stack=summary.tech_stack,
        active_projects=summary.active_projects,
        glossary_terms=summary.glossary_terms,
        recent_activity_summary=summary.recent_activity_summary,
        topic_graph_edges=summary.topic_graph_edges,
    )


@router.get("/api/entities/{entity_name}/card", response_model=EntityCardResponse)
async def get_entity_card(entity_name: str) -> EntityCardResponse:
    """Get the knowledge card for a named entity."""
    stores = get_stores()
    card = await stores.weaviate.get_entity_card(entity_name)
    if card is None:
        raise HTTPException(status_code=404, detail=f"No knowledge card for entity {entity_name}")
    return EntityCardResponse(
        entity_name=card.entity_name,
        entity_type=card.entity_type,
        channel_ids=card.channel_ids,
        cluster_ids=card.cluster_ids,
        fact_count=card.fact_count,
        fact_type_breakdown=card.fact_type_breakdown,
        key_facts=card.key_facts,
        related_entities=card.related_entities,
        last_mentioned_at=card.last_mentioned_at,
        staleness_score=card.staleness_score,
        summary=card.summary,
    )


@router.get("/api/entities/cards", response_model=list[EntityCardResponse])
async def list_entity_cards(channel_id: str | None = None) -> list[EntityCardResponse]:
    """List all entity knowledge cards, optionally filtered by channel."""
    stores = get_stores()
    cards = await stores.weaviate.list_entity_cards(channel_id=channel_id, limit=50)
    return [
        EntityCardResponse(
            entity_name=card.entity_name,
            entity_type=card.entity_type,
            channel_ids=card.channel_ids,
            cluster_ids=card.cluster_ids,
            fact_count=card.fact_count,
            fact_type_breakdown=card.fact_type_breakdown,
            key_facts=card.key_facts,
            related_entities=card.related_entities,
            last_mentioned_at=card.last_mentioned_at,
            staleness_score=card.staleness_score,
            summary=card.summary,
        )
        for card in cards
    ]


@router.post(
    "/api/channels/{channel_id}/consolidate",
    status_code=202,
)
async def trigger_consolidation(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> dict:
    """Trigger a full reconsolidation as a background task."""
    from beever_atlas.infra.config import get_settings
    from beever_atlas.services.consolidation import ConsolidationService

    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    settings = get_settings()
    service = ConsolidationService(stores.weaviate, settings, graph=stores.graph)

    async def _run() -> None:
        try:
            channel_name = (
                await stores.mongodb.get_channel_display_name(channel_id) or ""
            )
            result = await service.full_reconsolidate(
                channel_id, channel_name=channel_name,
            )
            logger.info(
                "Consolidation complete channel=%s created=%d updated=%d facts=%d errors=%d",
                channel_id, result.clusters_created, result.clusters_updated,
                result.facts_clustered, len(result.errors),
            )
        except Exception as exc:
            logger.error("Consolidation task failed channel=%s: %s", channel_id, exc, exc_info=True)

    asyncio.create_task(_run())

    return {"status": "started", "channel_id": channel_id}
