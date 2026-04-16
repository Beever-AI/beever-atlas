"""Knowledge graph API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.stores import get_stores
from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph

router = APIRouter(prefix="/api/graph", tags=["graph"])


@router.get("/entities", response_model=list[GraphEntity])
async def list_entities(
    channel_id: str | None = Query(default=None),
    entity_type: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(require_user),
) -> list[GraphEntity]:
    """List entities in the knowledge graph."""
    if channel_id:
        await assert_channel_access(principal, channel_id)
    stores = get_stores()
    return await stores.graph.list_entities(channel_id=channel_id, entity_type=entity_type, limit=limit)


@router.get("/relationships", response_model=list[GraphRelationship])
async def list_relationships(
    channel_id: str | None = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    principal: Principal = Depends(require_user),
) -> list[GraphRelationship]:
    """List relationships in the knowledge graph, including entity-media links."""
    if channel_id:
        await assert_channel_access(principal, channel_id)
    stores = get_stores()
    entity_rels = await stores.graph.list_relationships(channel_id=channel_id, limit=limit)

    # Also fetch entity→media relationships and convert to GraphRelationship
    media_rels_raw = await stores.graph.list_media_relationships(channel_id=channel_id, limit=100)
    for mr in media_rels_raw:
        entity_rels.append(GraphRelationship(
            type=mr["type"],
            source=mr["source"],
            target=mr["target"],
        ))

    return entity_rels


@router.get("/entities/{entity_id}/neighbors", response_model=Subgraph)
async def get_entity_neighbors(
    entity_id: str,
    hops: int = Query(default=1, ge=1, le=5),
    limit: int = Query(default=50, ge=1, le=500),
) -> Subgraph:
    """Get the N-hop neighborhood subgraph for an entity."""
    stores = get_stores()
    return await stores.graph.get_neighbors(entity_id, hops=hops, limit=limit)


@router.get("/media", response_model=list[dict])
async def list_media(
    channel_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    principal: Principal = Depends(require_user),
) -> list[dict]:
    """List media nodes in the knowledge graph."""
    if channel_id:
        await assert_channel_access(principal, channel_id)
    stores = get_stores()
    return await stores.graph.list_media(channel_id=channel_id, limit=limit)


@router.get("/decisions/{channel_id}", response_model=list[GraphEntity])
async def get_decisions(
    channel_id: str,
    limit: int = Query(default=20, ge=1, le=200),
    principal: Principal = Depends(require_user),
) -> list[GraphEntity]:
    """Get the decision timeline for a channel."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    return await stores.graph.get_decisions(channel_id, limit=limit)
