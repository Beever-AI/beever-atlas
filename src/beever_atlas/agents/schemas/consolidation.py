from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


def _coerce_str_list(value):
    """Coerce LLM-emitted string into list[str] when the model expects a list.

    Gemini/other LLMs occasionally emit comma- or newline-separated text
    for a list[str] field despite schema instructions. Split and strip
    instead of hard-failing Pydantic validation — losing the field is worse
    than normalising it.
    """
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        return [p for p in parts if p]
    return value


class SummaryResult(BaseModel):
    """Legacy output schema for the consolidation summarizer agent."""

    summary_text: str = ""
    """Generated summary text for a topic cluster or channel."""


# -- Sub-schemas --


class FaqCandidate(BaseModel):
    """A Q&A pair generated from cluster content."""

    question: str = ""
    answer: str = ""


class GlossaryTerm(BaseModel):
    """A channel-specific term with definition."""

    term: str = ""
    definition: str = ""
    first_mentioned_by: str = ""
    related_topics: list[str] = Field(default_factory=list)

    _coerce_related_topics = field_validator("related_topics", mode="before")(_coerce_str_list)


# -- Structured output schemas --


class TopicSummaryResult(BaseModel):
    """Structured LLM output for topic-level summarization."""

    title: str = ""
    """Short descriptive name, 5-10 words."""

    summary_text: str = ""
    """2-3 sentence narrative of what happened."""

    current_state: str = ""
    """1-2 sentences on where things stand now."""

    open_questions: str = ""
    """1-2 sentences on unresolved tensions, or empty string."""

    impact_note: str = ""
    """1 sentence on scope and significance."""

    topic_tags: list[str] = Field(default_factory=list)
    """3 most representative tags selected from member tags."""

    faq_candidates: list[FaqCandidate] = Field(default_factory=list)
    """0-3 Q&A pairs from cluster content."""

    _coerce_topic_tags = field_validator("topic_tags", mode="before")(_coerce_str_list)


class ChannelSummaryResult(BaseModel):
    """Structured LLM output for channel-level summarization."""

    summary_text: str = ""
    """3-5 sentence channel overview narrative."""

    description: str = ""
    """One-line purpose statement, max 200 chars."""

    themes: str = ""
    """2-3 sentences on main knowledge areas and how they interrelate."""

    momentum: str = ""
    """1-2 sentences on what's active vs. completed, recent velocity."""

    team_dynamics: str = ""
    """1-2 sentences on who drives decisions, collaboration patterns."""

    glossary_terms: list[GlossaryTerm] = Field(default_factory=list)
    """0-10 channel-specific jargon with definitions."""
