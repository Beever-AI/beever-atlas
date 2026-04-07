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
    source_media_url: str = ""  # Deprecated: use source_media_urls
    source_media_type: str = ""  # "image", "pdf", "doc", "video", ""
    source_media_urls: list[str] = Field(default_factory=list)
    source_media_names: list[str] = Field(default_factory=list)
    source_link_urls: list[str] = Field(default_factory=list)
    source_link_titles: list[str] = Field(default_factory=list)
    source_link_descriptions: list[str] = Field(default_factory=list)
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    superseded_by: str | None = None
    supersedes: str | None = None
    potential_contradiction: bool = False
    text_vector: list[float] | None = None
    fact_type: str = ""  # "decision", "opinion", "observation", "action_item", "question"
    thread_context_summary: str = ""  # Brief summary of thread deliberation

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
    status: str = "active"  # "active" or "pending"
    pending_since: datetime | None = None
    name_vector: list[float] | None = None
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


class TopicCluster(BaseModel):
    """A Tier 1 topic cluster grouping related atomic facts."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tier: str = "topic"
    channel_id: str
    # Multi-angle summary fields
    title: str = ""  # Short descriptive name (5-10 words)
    summary: str = ""  # Narrative of what happened (2-3 sentences)
    current_state: str = ""  # Where things stand now (1-2 sentences)
    open_questions: str = ""  # Unresolved tensions/debates (1-2 sentences, or empty)
    impact_note: str = ""  # Scope and significance (1 sentence)
    topic_tags: list[str] = Field(default_factory=list)
    member_ids: list[str] = Field(default_factory=list)
    member_count: int = 0
    centroid_vector: list[float] | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    # Enrichment fields (R4)
    key_entities: list[dict[str, str]] = Field(default_factory=list)  # [{"id", "name", "type"}]
    key_relationships: list[dict[str, str]] = Field(default_factory=list)  # [{"source", "type", "target", "confidence"}]
    date_range_start: str = ""
    date_range_end: str = ""
    authors: list[str] = Field(default_factory=list)
    media_refs: list[str] = Field(default_factory=list)
    media_names: list[str] = Field(default_factory=list)
    link_refs: list[str] = Field(default_factory=list)
    high_importance_count: int = 0
    related_cluster_ids: list[str] = Field(default_factory=list)
    staleness_score: float = 0.0  # 0.0=fresh, 1.0=very stale
    status: str = "active"  # "active", "completed", "stale"
    fact_type_counts: dict[str, int] = Field(default_factory=dict)  # {"decision": N, ...}
    # Wiki-ready enrichment fields
    key_facts: list[dict[str, Any]] = Field(default_factory=list)
    # [{"fact_id", "memory_text", "author_name", "message_ts", "fact_type", "importance", "quality_score", "source_message_id"}]
    decisions: list[dict[str, Any]] = Field(default_factory=list)
    # [{"name", "decided_by", "status", "superseded_by", "date", "context"}]
    people: list[dict[str, str]] = Field(default_factory=list)
    # [{"name", "role", "entity_id"}]  role: decision_maker|contributor|expert|mentioned
    technologies: list[dict[str, str]] = Field(default_factory=list)
    # [{"name", "category", "champion"}]
    projects: list[dict[str, Any]] = Field(default_factory=list)
    # [{"name", "status", "owner", "blockers"}]
    faq_candidates: list[dict[str, str]] = Field(default_factory=list)
    # [{"question", "answer"}]


class ChannelSummary(BaseModel):
    """A Tier 0 channel-level summary consolidating all topic clusters."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tier: str = "summary"
    channel_id: str
    # Multi-angle summary fields
    channel_name: str = ""  # Resolved display name (e.g. "#backend-engineering")
    text: str = ""  # Overall narrative overview (3-5 sentences)
    description: str = ""  # One-line channel purpose (max 200 chars)
    themes: str = ""  # Main knowledge areas and how they interrelate (2-3 sentences)
    momentum: str = ""  # What's active vs. completed vs. stale (1-2 sentences)
    team_dynamics: str = ""  # Who drives decisions, collaboration patterns (1-2 sentences)
    cluster_count: int = 0
    fact_count: int = 0
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    # Enrichment fields (R4)
    key_decisions: list[dict[str, str]] = Field(default_factory=list)
    key_entities: list[dict[str, str]] = Field(default_factory=list)
    key_topics: list[dict[str, Any]] = Field(default_factory=list)
    date_range_start: str = ""
    date_range_end: str = ""
    media_count: int = 0
    author_count: int = 0
    worst_staleness: float = 0.0
    # Wiki-ready enrichment fields
    top_decisions: list[dict[str, Any]] = Field(default_factory=list)
    # [{"name", "decided_by", "status", "superseded_by", "date", "topic_cluster_id", "context"}]
    top_people: list[dict[str, Any]] = Field(default_factory=list)
    # [{"name", "role", "topic_count", "expertise_topics"}]
    tech_stack: list[dict[str, Any]] = Field(default_factory=list)
    # [{"name", "category", "champion", "topic_count"}]
    active_projects: list[dict[str, Any]] = Field(default_factory=list)
    # [{"name", "status", "owner", "blockers", "topic_cluster_id"}]
    glossary_terms: list[dict[str, Any]] = Field(default_factory=list)
    # [{"term": str, "definition": str, "first_mentioned_by": str, "related_topics": list[str]}]
    recent_activity_summary: dict[str, Any] = Field(default_factory=dict)
    # {"facts_added_7d", "decisions_added_7d", "entities_added_7d", "new_topics", "updated_topics", "highlights"}
    topic_graph_edges: list[dict[str, Any]] = Field(default_factory=list)
    # [{"source_cluster_id", "target_cluster_id", "source_title", "target_title", "shared_entities"}]


class EntityKnowledgeCard(BaseModel):
    """Cross-channel aggregation of all knowledge about a single graph entity."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tier: str = "entity_card"
    entity_id: str = ""
    entity_name: str = ""
    entity_type: str = ""
    channel_ids: list[str] = Field(default_factory=list)
    cluster_ids: list[str] = Field(default_factory=list)
    fact_count: int = 0
    fact_type_breakdown: dict[str, int] = Field(default_factory=dict)
    key_facts: list[str] = Field(default_factory=list)
    related_entities: list[dict[str, str]] = Field(default_factory=list)
    last_mentioned_at: str = ""
    staleness_score: float = 0.0
    summary: str = ""
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
