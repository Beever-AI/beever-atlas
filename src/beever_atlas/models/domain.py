"""Domain models: core graph and fact entities."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class AtomicFact(BaseModel):
    """A single extracted fact stored in Weaviate (Tier 2)."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    memory_text: str
    quality_score: float = 0.0
    tier: str = "atomic"
    cluster_id: str | None = None
    channel_id: str = ""
    platform: str = "slack"
    author_id: str = ""
    author_name: str = ""
    message_ts: str = ""
    thread_ts: str | None = None
    source_message_id: str = ""
    topic_tags: list[str] = Field(default_factory=list)
    entity_tags: list[str] = Field(default_factory=list)
    action_tags: list[str] = Field(default_factory=list)
    importance: str = "medium"
    graph_entity_ids: list[str] = Field(default_factory=list)
    source_media_url: str = ""
    source_media_type: str = ""  # "image", "pdf", "doc", "video", ""
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    text_vector: list[float] | None = None

    @staticmethod
    def deterministic_id(platform: str, channel_id: str, message_ts: str, fact_index: int = 0) -> str:
        """Generate a deterministic UUID for idempotent upserts."""
        namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        return str(uuid.uuid5(namespace, f"{platform}:{channel_id}:{message_ts}:{fact_index}"))


class GraphEntity(BaseModel):
    """An entity node in the Neo4j knowledge graph."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    type: str  # Person, Decision, Project, Technology, etc.
    scope: str = "global"  # "global" or "channel"
    channel_id: str | None = None
    properties: dict[str, Any] = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list)
    source_fact_ids: list[str] = Field(default_factory=list)
    source_message_id: str = ""
    message_ts: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class GraphRelationship(BaseModel):
    """A relationship edge in the Neo4j knowledge graph."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: str  # DECIDED, WORKS_ON, USES, etc.
    source: str  # Source entity name
    target: str  # Target entity name
    confidence: float = 0.0
    valid_from: str | None = None
    valid_until: str | None = None
    context: str = ""
    source_message_id: str = ""
    source_fact_id: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class Subgraph(BaseModel):
    """A subgraph returned from Neo4j traversal queries."""

    nodes: list[GraphEntity] = Field(default_factory=list)
    edges: list[GraphRelationship] = Field(default_factory=list)
