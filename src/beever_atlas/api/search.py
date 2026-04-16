"""Search API — semantic and hybrid fact search."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["search"])


class SearchRequest(BaseModel):
    """Search request body."""
    query: str
    channel_id: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    threshold: float = Field(default=0.7, ge=0.0, le=1.0)


class SearchResultItem(BaseModel):
    """A single search result."""
    id: str = ""
    memory_text: str = ""
    quality_score: float = 0.0
    topic_tags: list[str] = Field(default_factory=list)
    entity_tags: list[str] = Field(default_factory=list)
    importance: str = "medium"
    author_name: str = ""
    message_ts: str = ""
    channel_id: str = ""
    similarity_score: float = 0.0


class SearchResponse(BaseModel):
    """Search response."""
    results: list[SearchResultItem] = Field(default_factory=list)
    total: int = 0
    query: str = ""


@router.post("/search", response_model=SearchResponse)
async def search_facts(
    body: SearchRequest,
    principal: Principal = Depends(require_user),
) -> SearchResponse:
    """Search facts using semantic similarity.

    Computes a query embedding via Jina, then runs hybrid search
    (vector + field-filter) against Weaviate.
    """
    if body.channel_id:
        await assert_channel_access(principal, body.channel_id)
    stores = get_stores()

    try:
        # Compute query embedding
        query_vector = await stores.entity_registry.compute_name_embedding(body.query)
    except Exception as exc:
        logger.warning("Search: embedding computation failed: %s", exc)
        raise HTTPException(status_code=503, detail="Embedding service unavailable") from exc

    try:
        raw_results = await stores.weaviate.pseudo_hybrid_search(
            query_vector=query_vector,
            channel_id=body.channel_id or "",
            limit=body.limit,
            threshold=body.threshold,
        )
    except Exception as exc:
        logger.error("Search: hybrid search failed: %s", exc)
        raise HTTPException(status_code=500, detail="Search failed") from exc

    items: list[SearchResultItem] = []
    for r in raw_results:
        fact = r["fact"]
        items.append(SearchResultItem(
            id=fact.id,
            memory_text=fact.memory_text,
            quality_score=fact.quality_score,
            topic_tags=fact.topic_tags,
            entity_tags=fact.entity_tags,
            importance=fact.importance,
            author_name=fact.author_name,
            message_ts=fact.message_ts,
            channel_id=fact.channel_id,
            similarity_score=r.get("similarity_score", 0.0),
        ))

    return SearchResponse(
        results=items,
        total=len(items),
        query=body.query,
    )
