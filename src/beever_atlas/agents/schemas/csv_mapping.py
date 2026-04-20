"""Pydantic schema for LLM-inferred CSV/JSONL column mappings."""

from __future__ import annotations

from pydantic import BaseModel, Field


class LLMColumnMapping(BaseModel):
    """LLM output for ``agents.ingestion.csv_mapper``.

    Each field names a source column from the uploaded file, or None if the
    model could not confidently identify one. The model is also asked to
    report a 0.0–1.0 confidence score for each field and optionally identify
    the source tool/format.
    """

    content: str = Field(
        description="The column containing the message body text. REQUIRED.",
    )
    author: str | None = Field(
        default=None,
        description="Column with a stable author identifier (ID/handle).",
    )
    author_name: str | None = Field(
        default=None,
        description="Column with the display name. May equal `author` if only one author field exists.",
    )
    timestamp: str | None = Field(
        default=None,
        description="Column with the message timestamp (date or datetime).",
    )
    timestamp_time: str | None = Field(
        default=None,
        description="Optional second column when date and time are split (e.g. WhatsApp 'Date' + 'Time').",
    )
    message_id: str | None = Field(
        default=None,
        description="Unique message identifier column, if present.",
    )
    thread_id: str | None = Field(
        default=None,
        description="Parent thread / reply-to identifier column, if present.",
    )
    attachments: str | None = None
    reactions: str | None = None

    confidence: dict[str, float] = Field(
        default_factory=dict,
        description="Per-field confidence 0.0–1.0. Keys match field names.",
    )
    detected_source: str | None = Field(
        default=None,
        description="Optional human-readable guess at the export tool (e.g. 'DiscordChatExporter', 'Slack export').",
    )
    notes: str = Field(
        default="",
        description="Short free-text explanation of ambiguous cases.",
    )
