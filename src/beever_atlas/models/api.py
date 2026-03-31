"""API models: query filters, paginated responses, and health."""

from __future__ import annotations

from pydantic import BaseModel, Field

from beever_atlas.models.domain import AtomicFact


class MemoryFilters(BaseModel):
    """Query filters for memories API."""

    topic: str | None = None
    entity: str | None = None
    importance: str | None = None
    since: str | None = None
    until: str | None = None


class PaginatedFacts(BaseModel):
    """Paginated response for memories API."""

    memories: list[AtomicFact] = Field(default_factory=list)
    total: int = 0
    page: int = 1
    pages: int = 1


class ComponentHealth(BaseModel):
    """Health status for a single dependency."""

    status: str  # "up" or "down"
    latency_ms: float
    error: str | None = None


class HealthResponse(BaseModel):
    """Overall system health response for GET /api/health."""

    status: str  # "healthy", "degraded", or "unhealthy"
    components: dict[str, ComponentHealth]
    checked_at: str  # ISO 8601 timestamp
