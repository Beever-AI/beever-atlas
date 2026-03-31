"""Memory (atomic facts) API endpoints."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Query

from beever_atlas.stores import get_stores
from beever_atlas.models import MemoryFilters, PaginatedFacts, AtomicFact

router = APIRouter(prefix="/api/channels", tags=["memories"])


@router.get("/{channel_id}/memories", response_model=PaginatedFacts)
async def list_memories(
    channel_id: str,
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=20, ge=1, le=200),
    topic: str | None = Query(default=None),
    entity: str | None = Query(default=None),
    importance: str | None = Query(default=None),
    since: str | None = Query(default=None),
    until: str | None = Query(default=None),
) -> PaginatedFacts:
    """List atomic facts for a channel with optional filters."""
    stores = get_stores()
    filters = MemoryFilters(
        topic=topic,
        entity=entity,
        importance=importance,
        since=since,
        until=until,
    )
    return await stores.weaviate.list_facts(channel_id, filters=filters, page=page, limit=limit)


@router.get("/{channel_id}/memories/{memory_id}")
async def get_memory(channel_id: str, memory_id: str) -> dict:
    """Get a single atomic fact by ID, enriched with graph entity details if available."""
    stores = get_stores()
    fact = await stores.weaviate.get_fact(memory_id)
    if fact is None:
        raise HTTPException(status_code=404, detail=f"Memory {memory_id} not found")
    enriched = fact.model_dump()
    if fact.graph_entity_ids:
        entities = await asyncio.gather(*[stores.neo4j.get_entity(eid) for eid in fact.graph_entity_ids])
        enriched["linked_entities"] = [e.model_dump() for e in entities if e is not None]
    return enriched
