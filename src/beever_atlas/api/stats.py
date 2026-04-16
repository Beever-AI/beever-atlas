"""Aggregate stats and activity feed API endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.stores import get_stores

router = APIRouter(prefix="/api", tags=["stats"])


@router.get("/stats")
async def get_stats() -> dict:
    """Return aggregate stats across all stores."""
    stores = get_stores()
    total_memories = await stores.weaviate.count_facts()
    total_entities = await stores.graph.count_entities()
    total_relationships = await stores.graph.count_relationships()
    channels_synced = await stores.mongodb.count_synced_channels()
    last_sync_at = await stores.mongodb.get_last_sync_timestamp()
    return {
        "total_memories": total_memories,
        "total_entities": total_entities,
        "total_relationships": total_relationships,
        "channels_synced": channels_synced,
        "last_sync_at": last_sync_at,
    }


@router.get("/activity")
async def get_activity(
    limit: int = Query(default=20, ge=1, le=200),
) -> list[dict]:
    """Return recent activity events."""
    stores = get_stores()
    events = await stores.mongodb.get_recent_activity(limit=limit)
    return [e if isinstance(e, dict) else e.model_dump() for e in events]


@router.get("/sync-history")
async def get_sync_history(
    channel_id: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(require_user),
) -> list[dict]:
    """Return sync history with per-batch extraction breakdowns."""
    if channel_id:
        await assert_channel_access(principal, channel_id)
    stores = get_stores()
    events = await stores.mongodb.get_sync_history(
        channel_id=channel_id, limit=limit,
    )
    return [e if isinstance(e, dict) else e.model_dump() for e in events]
