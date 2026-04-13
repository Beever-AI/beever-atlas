from __future__ import annotations

from pydantic import BaseModel, Field


class ExtractedFact(BaseModel):
    """A single discrete fact extracted from one Slack message."""

    memory_text: str
    """Self-contained natural-language statement of the fact."""

    quality_score: float
    """0.0–1.0 composite score: specificity × actionability × verifiability."""

    topic_tags: list[str] = Field(default_factory=list)
    """Thematic labels (e.g. "deployment", "auth", "roadmap")."""

    entity_tags: list[str] = Field(default_factory=list)
    """Named entities mentioned in the fact (people, projects, tools)."""

    action_tags: list[str] = Field(default_factory=list)
    """Action-oriented labels (e.g. "decided", "blocked", "completed")."""

    importance: str = "medium"
    """One of: "low", "medium", "high", "critical"."""

    source_message_id: str = ""
    """Slack message ``ts`` that this fact was extracted from."""

    author_id: str = ""
    """Slack user ID of the message author."""

    author_name: str = ""
    """Display name of the message author."""

    message_ts: str = ""
    """ISO-8601 or Slack epoch timestamp of the source message."""

    fact_type: str = "observation"
    """One of: "decision", "opinion", "observation", "action_item", "question"."""

    thread_context_summary: str = ""
    """Brief summary of thread deliberation arc when fact comes from a threaded discussion."""

    source_lang: str = "en"
    """BCP-47 language tag of the source message this fact was extracted from
    (e.g. "en", "zh-HK", "ja"). Preserved so wiki/QA rendering can translate
    on demand. Defaults to "en" for backwards compatibility with pre-change data.
    """


class FactExtractionResult(BaseModel):
    """Top-level LLM output for one batch of preprocessed messages."""

    facts: list[ExtractedFact] = Field(default_factory=list)
    """All extracted facts across the batch."""

    skip_reason: str | None = None
    """If set, the LLM determined extraction was not worthwhile (e.g. spam)."""


class ExtractedEntity(BaseModel):
    """A named entity identified in the message batch."""

    name: str
    """Canonical name for this entity (e.g. "Alice Smith", "Atlas API", "Redis")."""

    type: str
    """Entity type: Person | Decision | Project | Technology | Team | Meeting | Artifact."""

    scope: str = "global"
    """
    Scoping rule:
    - "global"  → Person, Technology, Project, Team (relevant across all channels)
    - "channel" → Decision, Meeting, Artifact (scoped to the channel where they appear)
    """

    class EntityProperties(BaseModel):
        """Structured optional metadata for common entity types."""

        role: str | None = None
        team: str | None = None
        email: str | None = None
        version: str | None = None
        language: str | None = None
        category: str | None = None
        status: str | None = None
        repo: str | None = None
        owner: str | None = None
        rationale: str | None = None
        alternatives_considered: str | None = None
        decided_by: str | None = None
        visual_description: str | None = None

    properties: EntityProperties = Field(default_factory=EntityProperties)
    """Structured properties for supported entity metadata keys."""

    aliases: list[str] = Field(default_factory=list)
    """Alternative names or spellings observed in messages."""

    status: str = "active"
    """Entity lifecycle status: "active" or "pending" (orphan with no relationships)."""

    source_message_id: str = ""
    """Slack message ``ts`` where this entity was first identified in the batch."""

    source_lang: str = "en"
    """BCP-47 language tag of the source messages this entity was observed in.
    Used by the wiki/QA layer to preserve native-script names and render
    translated descriptions on demand.
    """


class ExtractedRelationship(BaseModel):
    """A directed relationship between two entities."""

    type: str
    """Verb-phrase relationship type in SCREAMING_SNAKE_CASE.

    Common types: DECIDED, WORKS_ON, USES, OWNS, BLOCKED_BY, REPORTS_TO,
    DEPENDS_ON, CREATED, REVIEWED, MERGED, DEPLOYED, SCHEDULED.
    """

    source: str
    """Canonical name of the source entity."""

    target: str
    """Canonical name of the target entity."""

    confidence: float = 0.0
    """0.0–1.0 confidence that this relationship is correct and current."""

    valid_from: str | None = None
    """ISO-8601 timestamp when the relationship became valid (from message context)."""

    context: str = ""
    """Short verbatim quote or paraphrase from the message supporting this relationship."""


class EntityExtractionResult(BaseModel):
    """Top-level LLM output for one batch of preprocessed messages."""

    entities: list[ExtractedEntity] = Field(default_factory=list)
    """All entities extracted from the batch."""

    relationships: list[ExtractedRelationship] = Field(default_factory=list)
    """All relationships extracted from the batch."""

    skip_reason: str | None = None
    """If set, the LLM determined extraction was not worthwhile."""
