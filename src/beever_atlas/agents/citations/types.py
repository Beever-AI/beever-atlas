"""Typed records for the citation registry.

These are deliberately plain dataclasses with serialize helpers rather than
pydantic models — they live on hot paths (per-tool-result) and we want to
avoid the validation overhead. Conversion to dict is explicit.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal

SupportedKind = Literal[
    "channel_message",
    "wiki_page",
    "qa_history",
    "uploaded_file",
    "web_result",
    "graph_relationship",
    "decision_record",
    "media",
]

MediaKind = Literal[
    "image",
    "pdf",
    "video",
    "audio",
    "link_preview",
    "document",
]


@dataclass(frozen=True)
class MediaAttachment:
    """A single piece of media evidence attached to a citation source."""

    kind: MediaKind
    url: str
    thumbnail_url: str | None = None
    mime_type: str | None = None
    filename: str | None = None
    title: str | None = None
    alt_text: str | None = None
    width: int | None = None
    height: int | None = None
    byte_size: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Source:
    """A citable source surfaced by a retrieval tool.

    `id` is a stable 10-char hex hash; same native identity across tool calls
    yields the same id. `excerpt` is the grounding text the user sees in
    hover/expand; capped at 400 chars by the registry on registration.
    """

    id: str
    kind: SupportedKind
    title: str
    excerpt: str
    retrieved_by: dict[str, Any]  # {tool, query, score}
    native: dict[str, Any]  # kind-specific payload
    attachments: list[MediaAttachment] = field(default_factory=list)
    permalink: str | None = None
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "excerpt": self.excerpt,
            "retrieved_by": self.retrieved_by,
            "native": self.native,
            "attachments": [a.to_dict() for a in self.attachments],
            "permalink": self.permalink,
            "created_at": self.created_at,
        }


@dataclass
class CitationRef:
    """A single inline citation reference — maps a [N] marker to a source."""

    marker: int
    source_id: str
    inline: bool = False
    ranges: list[dict[str, int]] = field(default_factory=list)  # [{start, end}]
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "marker": self.marker,
            "source_id": self.source_id,
            "inline": self.inline,
            "ranges": list(self.ranges),
            "note": self.note,
        }


@dataclass
class CitationEnvelope:
    """What the backend ships to the client and persists.

    `items` preserves the legacy flat citation shape so existing frontend
    consumers keep working during Phase 1 soak. `sources` + `refs` are the
    new structured contract.
    """

    items: list[dict[str, Any]]
    sources: list[Source]
    refs: list[CitationRef]

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": list(self.items),
            "sources": [s.to_dict() for s in self.sources],
            "refs": [r.to_dict() for r in self.refs],
        }

    @staticmethod
    def empty() -> "CitationEnvelope":
        return CitationEnvelope(items=[], sources=[], refs=[])
