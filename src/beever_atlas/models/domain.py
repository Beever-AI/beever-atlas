"""Domain models: core graph and fact entities."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator


class AtomicFact(BaseModel):
    """A single extracted fact stored in Weaviate (Tier 2)."""

    # Coerce ``None`` to empty string for plain-``str`` fields below.
    # The LLM occasionally emits ``null`` for ``thread_context_summary``
    # and a few other narrative fields; without this validator Pydantic
    # raises ``Input should be a valid string`` which the BatchProcessor
    # retry classifier treated as transient — wasting up to 11 minutes
    # per occurrence in the 5/30/90/180/360s backoff schedule (observed
    # in production logs 2026-05-11). Validation errors against
    # deterministic LLM output should fail fast OR coerce; we coerce.
    @field_validator(
        "thread_context_summary",
        "derived_from",
        "source_media_url",
        "source_media_type",
        mode="before",
    )
    @classmethod
    def _coerce_null_to_empty(cls, v: Any) -> Any:
        if v is None:
            return ""
        return v

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    memory_text: str
    quality_score: float = 0.0
    tier: str = "atomic"
    cluster_id: str | None = None
    channel_id: str = ""
    platform: str = "slack"
    guild_id: str = ""
    """Discord-only: the guild (server) id, stamped by the persister from the
    matched source message so the permalink resolver can build a clickable
    Discord message URL. Empty for non-Discord platforms (their URL templates
    do not need it). ``platform`` keeps its "slack" default; the persister
    overrides it explicitly only when the source message carries a platform."""
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
    source_lang: str = "en"  # BCP-47 tag of the source message (e.g. "en", "zh-HK", "ja")
    derived_from: str = (
        ""  # Provenance marker, e.g. "heuristic_word_overlap" for low-confidence attribution
    )

    # Phase 3 extraction enrichment — additive, all optional with safe
    # defaults so pre-Phase-3 documents deserialize cleanly without
    # migration. The fact extractor prompt populates these fields when
    # applicable; consumers (decision_banner, stat_strip, acronym_legend)
    # fall back to legacy behavior when fields are empty/null.
    rationale: str | None = None
    """For ``fact_type == "decision"``: the 'because' justification —
    a single sentence explaining WHY the decision was made. Null when
    the source message did not include explicit justification."""

    alternatives_considered: list[str] = Field(default_factory=list)
    """For ``fact_type == "decision"``: alternative options that were
    weighed and rejected. Each item is a 1-line description.
    Empty list when no alternatives were discussed in the source."""

    consequences_open: list[str] = Field(default_factory=list)
    """For ``fact_type == "decision"``: open questions raised about
    downstream effects of this decision. Each item is a 1-line
    question. Empty list when no consequences surfaced."""

    numeric_values: list[dict] = Field(default_factory=list)
    """Structured numeric extractions: each item is a dict with
    keys: ``label`` (str), ``value`` (str — display form, e.g. "2,396"),
    ``raw_value`` (int|float — for sorting/trends), ``unit`` (str|None).
    Surfaced via ``stat_strip``; falls back to regex when empty."""

    sentiment: str | None = None
    """For ``fact_type in {"opinion", "recommendation"}``: one of
    ``"neutral" | "concerning" | "positive" | "recommendation"``.
    Null for non-opinion facts. Surfaced via ``quote_highlights`` color."""

    glossary_terms: list[str] = Field(default_factory=list)
    """Acronyms (≥3 uppercase letters) or known domain terms that
    appear in this fact's body. Used by ``acronym_legend`` to filter
    the channel glossary to terms actually used. Empty list when no
    terms detected."""

    # Phase 4 tension detection — additive, optional, default-safe.
    # Populated at wiki compile time (not extraction time); existing
    # facts deserialize cleanly without these fields.
    tension_id: str | None = None
    """Stable identifier shared by 2+ facts that contradict each other.
    Format: ``"t_" + 8-char hash``. None when the fact is not part of
    a detected tension. Surfaced via ``tension_callout`` module."""

    contradicts_fact_id: str | None = None
    """When this fact is paired with a tension partner, points to the
    specific other fact's id. Surfaced as a cross-link in the tension
    callout's positions list."""

    @staticmethod
    def deterministic_id(memory_text: str, entity_names: list[str]) -> str:
        """Generate a content-derived deterministic UUID for idempotent upserts.

        Switched from a position-based key
        (``platform:channel_id:message_ts:fact_index``) to a content-derived
        hash. The position-based key shifted whenever the LLM produced facts
        in a different order or count on retry, causing phantom Weaviate
        duplicates. The content hash is stable across reorderings and partial
        failures.

        The same fact text + same entity set yields the same UUID; subtly
        different text or a different entity set yields a different UUID.
        Empty ``entity_names`` is permitted (some fact_types like
        ``"observation"`` may extract zero entities).

        Separator: ``\\x00`` (null byte). Pipe characters can legitimately
        appear in LLM-extracted memory_text (\"option A | option B\"), which
        would alias with the entity join separator and create deterministic
        collisions across unrelated facts. Null bytes cannot appear in
        natural-language text, so they are an unambiguous separator.
        """
        namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
        normalized_entities = "\x00".join(sorted(str(n) for n in entity_names))
        digest = hashlib.sha256(f"{memory_text}\x00{normalized_entities}".encode()).hexdigest()[:16]
        return str(uuid.uuid5(namespace, digest))


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
    source_lang: str = "en"  # BCP-47 tag — language of the messages this entity was observed in
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
    key_relationships: list[dict[str, str]] = Field(
        default_factory=list
    )  # [{"source", "type", "target", "confidence"}]
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
    # Summary freshness signal — flips True whenever ``_incremental_cluster``
    # adds new members (membership delta), flips False after a successful
    # ``_summarize_one`` run. ``_select_clusters_needing_summary`` filters
    # on this so re-firing ``memory_settled`` with no intervening membership
    # changes is a cheap no-op (idempotency contract).
    summary_dirty: bool = True
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


class WikiCitation(BaseModel):
    """A source citation for a wiki page fact."""

    id: str  # "[1]", "[2]", etc.
    fact_id: str = ""  # The underlying AtomicFact.id (e.g., "f_abc123") so
    # narrative inline-citation chips can resolve hover-popover content by
    # the fact_id markers the v3 prompt embeds in paragraph text.
    author: str = ""
    channel: str = ""
    timestamp: str = ""
    text_excerpt: str = ""  # First 100 chars of original message
    permalink: str = ""
    media_type: str | None = None  # "pdf", "image", "link", "video", "audio"
    media_name: str | None = None  # Filename or domain for media-sourced citations


class WikiPageRef(BaseModel):
    """Lightweight reference to a sub-page (used in children lists)."""

    id: str
    title: str
    slug: str
    section_number: str
    memory_count: int = 0


class WikiPage(BaseModel):
    """A single wiki page with enhanced Markdown content.

    ``page_type`` accepts ``"fixed"`` | ``"topic"`` | ``"sub-topic"`` |
    ``"folder"``. Folder pages are first-class wiki pages produced by
    the structure planner (``llm-wiki-folder-structure`` change) — they
    have their own synthesized index content AND a ``children`` list
    of immediate descendants. ``children_fingerprint`` lets the
    compiler/maintainer skip redundant folder-index re-synthesis when
    the child set is unchanged.
    """

    id: str  # "overview", "people", "topic-authentication", "topic-auth--jwt-migration", "folder-security"
    slug: str
    title: str
    page_type: str = "fixed"  # "fixed" | "topic" | "sub-topic" | "folder"
    parent_id: str | None = None
    section_number: str = ""  # "1", "2.1", "2.1.1", "2.1.1.1" (arbitrary-depth path)
    content: str = ""  # Enhanced Markdown (mermaid/chart/callout/media blocks)
    summary: str = ""  # 1-2 sentence summary for cards/tooltips
    memory_count: int = 0
    last_updated: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    citations: list[WikiCitation] = Field(default_factory=list)
    children: list[WikiPageRef] = Field(default_factory=list)

    # Folder-only metadata. Both default-None on non-folder pages so the
    # JSON shape stays compact for leaves (Pydantic's default
    # ``model_dump`` includes None fields, but the persistence layer
    # filters those out before write — see WikiPageStore.save_page).
    children_fingerprint: str | None = None
    is_synthetic: bool = False

    # Adaptive page module plan (``adaptive-wiki-page-content`` change).
    # Each entry is at minimum ``{"id": str, "anchor": str}`` with an
    # optional ``"data": dict`` payload the maintainer reads for
    # surgical patching. Empty list = legacy page; renderer falls back
    # to the single-template flow over ``content``.
    modules: list[dict[str, Any]] = Field(default_factory=list)

    # ``wiki-narrative-articles`` — multi-section narrative article
    # body produced by the v3 ``MODULE_COMPILE_PROMPT_V3``. Each entry
    # is ``{anchor, heading, paragraphs: [{text, citations[],
    # is_inference}], citations[], visual: dict | None,
    # citation_coverage: float}``. Mirrors ``persistence.WikiPage.
    # narrative_sections`` so the domain → ``model_dump`` →
    # ``wiki_cache`` round-trip carries the field through to the per-
    # page store. Empty list means the page predates narrative
    # generation OR the validator rejected the LLM output and the
    # page falls back to module-only rendering.
    narrative_sections: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        """True when this page is a structure-planner folder (or
        hand-curated folder, future). Convenience for compile/render
        dispatch — keeps callers from string-comparing ``page_type``."""
        return self.page_type == "folder"


class WikiPageNode(BaseModel):
    """A node in the sidebar navigation tree (recursive)."""

    id: str
    title: str
    slug: str
    section_number: str
    page_type: str = "fixed"  # "fixed" | "topic" | "sub-topic" | "folder"
    memory_count: int = 0
    children: list["WikiPageNode"] = Field(default_factory=list)
    is_synthetic: bool = False
    # Optional one-line summary (1-2 sentences) used by the Overview's
    # topic-card grid so each card can show a brief description without
    # the user having to click through. Populated by the structure
    # serialiser from the corresponding WikiPage.summary field.
    summary: str = ""


class WikiStructure(BaseModel):
    """Sidebar navigation tree — lightweight, no page content."""

    channel_id: str
    channel_name: str = ""
    platform: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    is_stale: bool = False
    pages: list[WikiPageNode] = Field(default_factory=list)


class WikiMetadata(BaseModel):
    """Metadata about the wiki generation."""

    member_count: int = 0
    message_count: int = 0
    memory_count: int = 0
    entity_count: int = 0
    media_count: int = 0
    page_count: int = 0
    generation_cost_usd: float = 0.0
    generation_duration_ms: int = 0


class WikiResponse(BaseModel):
    """Full response from GET /wiki — structure + overview page."""

    channel_id: str
    channel_name: str = ""
    platform: str = ""
    generated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    is_stale: bool = False
    structure: WikiStructure
    overview: WikiPage
    metadata: WikiMetadata


class WikiVersionSummary(BaseModel):
    """Lightweight metadata for a wiki version (used in list responses)."""

    version_number: int
    channel_id: str
    generated_at: datetime
    archived_at: datetime
    page_count: int = 0
    model: str = ""


class WikiVersion(BaseModel):
    """Full snapshot of a wiki at a point in time."""

    version_number: int
    channel_id: str
    channel_name: str = ""
    platform: str = ""
    generated_at: datetime
    archived_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    page_count: int = 0
    model: str = ""
    structure: dict = Field(default_factory=dict)
    overview: dict = Field(default_factory=dict)
    pages: dict = Field(default_factory=dict)
    metadata: dict = Field(default_factory=dict)


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
