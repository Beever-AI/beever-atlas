"""Citation registry — Phase 1 of the enterprise citation architecture.

Replaces the legacy LLM-prose-tail + regex-parser antipattern with a
session-scoped SourceRegistry fed by tool outputs. The LLM emits opaque
[src:xxx] tags; the backend rewrites them to [N] at stream time and ships
typed Source/CitationRef records alongside the legacy shape.

Gated behind `settings.citation_registry_enabled`. When the flag is off,
every public API here is a no-op so the legacy path runs unchanged.
"""

from beever_atlas.agents.citations.types import (
    CitationEnvelope,
    CitationRef,
    MediaAttachment,
    MediaKind,
    Source,
    SupportedKind,
)

__all__ = [
    "CitationEnvelope",
    "CitationRef",
    "MediaAttachment",
    "MediaKind",
    "Source",
    "SupportedKind",
]
