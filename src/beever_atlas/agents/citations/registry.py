"""Session-scoped SourceRegistry.

Bound at the top of an agent turn via a ContextVar. Tool decorators read
the current registry and register sources without explicit plumbing. At
turn completion, `finalize()` returns a CitationEnvelope containing only
the subset the LLM actually referenced via [src:...] tags.
"""

from __future__ import annotations

import hashlib
import logging
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Iterable

from beever_atlas.agents.citations.types import (
    CitationEnvelope,
    CitationRef,
    MediaAttachment,
    Source,
    SupportedKind,
)

logger = logging.getLogger(__name__)

_EXCERPT_CAP = 400

# The current-turn registry. Agents/tools read via `current_registry()`.
# Set at the top of _run_agent_stream and reset in its finally block.
_current: ContextVar["SourceRegistry | None"] = ContextVar("citation_source_registry", default=None)


def current_registry() -> "SourceRegistry | None":
    """Return the registry bound to the current task context, or None."""
    return _current.get()


@dataclass
class _MarkerRecord:
    marker: int
    inline: bool


@dataclass
class SourceRegistry:
    """Collects Sources produced during one agent turn.

    - `register()` is idempotent on source_id; repeated registrations
      update retrieval score max-wise and merge attachments.
    - `mark_referenced()` records a [N] marker assignment with optional
      inline flag. Called by the stream rewriter.
    - `finalize()` builds the CitationEnvelope, dropping unused sources.
    """

    session_id: str = ""
    _sources: dict[str, Source] = field(default_factory=dict)
    _markers: dict[str, _MarkerRecord] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)  # first-appearance order
    _permalink_resolver: Any = None  # injected lazily to avoid import cycles

    # ---- registration --------------------------------------------------

    def register(
        self,
        *,
        kind: SupportedKind,
        native_identity: str,
        native: dict[str, Any],
        title: str,
        excerpt: str,
        retrieved_by: dict[str, Any],
        attachments: Iterable[MediaAttachment] = (),
    ) -> str | None:
        """Register a source; return its stable id, or None if excerpt is empty."""
        excerpt = _truncate_excerpt(excerpt)
        if not excerpt:
            logger.debug(
                "citation.register skipped: empty excerpt (kind=%s, identity=%s)",
                kind,
                native_identity,
            )
            return None

        source_id = _derive_id(kind, native_identity)
        attachments_list = list(attachments)

        if source_id in self._sources:
            existing = self._sources[source_id]
            # Merge attachments (dedup by url, preserve first-seen order).
            seen = {a.url for a in existing.attachments}
            for a in attachments_list:
                if a.url and a.url not in seen:
                    existing.attachments.append(a)
                    seen.add(a.url)
            # Max-wise score accumulation.
            new_score = retrieved_by.get("score")
            old_score = existing.retrieved_by.get("score")
            if _is_higher(new_score, old_score):
                existing.retrieved_by["score"] = new_score
            return source_id

        self._sources[source_id] = Source(
            id=source_id,
            kind=kind,
            title=title or kind,
            excerpt=excerpt,
            retrieved_by=dict(retrieved_by),
            native=dict(native),
            attachments=attachments_list,
            permalink=None,  # resolved in finalize()
            created_at=datetime.now(UTC).isoformat(timespec="seconds"),
        )
        return source_id

    # ---- marker assignment --------------------------------------------

    def mark_referenced(self, source_id: str, marker: int, inline: bool = False) -> bool:
        """Record a [N] marker → source mapping. Return True if source exists.

        If the caller requests `inline=True` on a source without attachments,
        the flag is downgraded to False and logged at debug level.
        """
        source = self._sources.get(source_id)
        if source is None:
            logger.warning("citation.mark_referenced: unknown source_id=%s", source_id)
            return False

        effective_inline = inline
        if inline and not source.attachments:
            logger.debug(
                "citation.mark_referenced: inline downgraded (source=%s, no attachments)",
                source_id,
            )
            effective_inline = False

        existing = self._markers.get(source_id)
        if existing is None:
            self._markers[source_id] = _MarkerRecord(marker=marker, inline=effective_inline)
            self._order.append(source_id)
        else:
            # Inline sticks once set true.
            if effective_inline and not existing.inline:
                existing.inline = True
        return True

    # ---- finalization --------------------------------------------------

    def finalize(self, _answer_text: str | None = None) -> CitationEnvelope:
        """Build the envelope. Drops unreferenced sources."""
        if not self._markers:
            return CitationEnvelope.empty()

        referenced_sources: list[Source] = []
        refs: list[CitationRef] = []
        for source_id in self._order:
            source = self._sources.get(source_id)
            if source is None:
                continue
            source.permalink = self._resolve_permalink(source)
            referenced_sources.append(source)
            mr = self._markers[source_id]
            refs.append(
                CitationRef(
                    marker=mr.marker,
                    source_id=source_id,
                    inline=mr.inline,
                )
            )

        items = _build_legacy_items(referenced_sources, refs)
        return CitationEnvelope(items=items, sources=referenced_sources, refs=refs)

    # ---- resolver injection -------------------------------------------

    def set_permalink_resolver(self, resolver: Any) -> None:
        """Inject a resolver (duck-typed: `resolve(source) -> str | None`)."""
        self._permalink_resolver = resolver

    def _resolve_permalink(self, source: Source) -> str | None:
        resolver = self._permalink_resolver
        if resolver is None:
            return None
        try:
            return resolver.resolve(source)
        except Exception:
            logger.warning("permalink resolver failed for source=%s", source.id, exc_info=True)
            return None

    # ---- introspection (observability + API) --------------------------

    def has_source(self, source_id: str) -> bool:
        """Public check used by the stream rewriter. Avoids cross-module
        access to the private `_sources` dict.
        """
        return source_id in self._sources

    @property
    def registered_count(self) -> int:
        return len(self._sources)

    @property
    def referenced_count(self) -> int:
        return len(self._markers)

    def permalink_null_by_kind(self) -> dict[str, int]:
        """For observability after finalize(); counts nulls per kind."""
        out: dict[str, int] = {}
        for s in self._sources.values():
            if s.permalink is None:
                out[s.kind] = out.get(s.kind, 0) + 1
        return out


# ----- ContextVar management -------------------------------------------


def bind(session_id: str = "") -> tuple[SourceRegistry, Token]:
    """Bind a fresh registry to the current context.

    Returns the registry and a reset token; callers MUST call reset()
    in a finally block.
    """
    registry = SourceRegistry(session_id=session_id)
    token = _current.set(registry)
    return registry, token


def reset(token: Token) -> None:
    _current.reset(token)


# ----- helpers ---------------------------------------------------------


def _derive_id(kind: str, native_identity: str) -> str:
    h = hashlib.sha1(f"{kind}|{native_identity}".encode("utf-8")).hexdigest()
    return f"src_{h[:10]}"


def _truncate_excerpt(text: str | None) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= _EXCERPT_CAP:
        return text
    cutoff = text[:_EXCERPT_CAP]
    # Prefer a word boundary.
    ws = cutoff.rfind(" ")
    if ws >= _EXCERPT_CAP * 0.6:
        cutoff = cutoff[:ws]
    return cutoff.rstrip() + "…"


def _is_higher(new: Any, old: Any) -> bool:
    try:
        if new is None:
            return False
        if old is None:
            return True
        return float(new) > float(old)
    except (TypeError, ValueError):
        return False


def _build_legacy_items(sources: list[Source], refs: list[CitationRef]) -> list[dict[str, Any]]:
    """Emit the legacy flat `items` shape from structured sources.

    Shape matches what `_extract_citations_from_text` produces today so
    existing frontend consumers keep working during Phase 1.
    """
    marker_by_source = {r.source_id: r.marker for r in refs}
    items: list[dict[str, Any]] = []
    for source in sources:
        marker = marker_by_source.get(source.id)
        native = source.native
        items.append(
            {
                "type": source.kind,
                "number": str(marker) if marker is not None else "",
                "author": native.get("author", ""),
                "channel": native.get("channel_name") or native.get("channel", ""),
                "timestamp": native.get("timestamp", ""),
                "text": source.excerpt,
                "permalink": source.permalink,
            }
        )
    return items
