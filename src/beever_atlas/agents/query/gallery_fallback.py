"""Safety-net media gallery appender.

If the `media-gallery` skill's template is ignored by the LLM, this module
injects a `## Media` section at the end of the answer built directly from
registered `media`-kind sources (and any `channel_message` source carrying
image/pdf/video attachments). Ensures the user always sees media the
retrieval layer found, regardless of formatting compliance.

Idempotent: returns empty string when the answer already contains a
`## Media` heading or no eligible sources exist.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beever_atlas.agents.citations.registry import SourceRegistry
    from beever_atlas.agents.citations.types import MediaAttachment, Source

logger = logging.getLogger(__name__)

_MEDIA_HEADING_RE = re.compile(r"(?im)^\s*##\s+Media\b")

_RENDERABLE_KINDS = {"image", "pdf", "video", "document"}
_MAX_GALLERY_ITEMS = 6


def maybe_build_gallery(registry: "SourceRegistry", answer_text: str) -> str:
    """Return a `## Media` section to append, or "" if none is needed.

    Scans the registry for sources with at least one renderable attachment
    (image/pdf/video/document). If the answer already has a `## Media`
    heading or no eligible sources are found, returns empty string.

    Each bullet is emitted with a freshly assigned `[N]` marker, and the
    corresponding source is marked as referenced with `inline=True` so the
    frontend renders the attachment inline.
    """
    if _MEDIA_HEADING_RE.search(answer_text):
        return ""

    eligible = list(_iter_eligible_sources(registry))
    if not eligible:
        return ""

    # Compute the next marker above any already-assigned one.
    existing_markers = {
        mr.marker for mr in registry._markers.values()  # noqa: SLF001
    }
    next_marker = max(existing_markers, default=0) + 1

    lines = ["", "", "## Media", ""]
    appended = 0
    for source, attachment in eligible[:_MAX_GALLERY_ITEMS]:
        caption = _caption_for(source, attachment)
        context = _context_for(source)
        marker = next_marker
        next_marker += 1
        ok = registry.mark_referenced(source.id, marker, inline=True)
        if not ok:
            continue
        lines.append(f"- ![{caption}]({attachment.url})")
        lines.append(f"  **{caption}** — {context} [{marker}]")
        appended += 1

    if appended == 0:
        return ""

    logger.info(
        "gallery_fallback: appended %d media item(s) to answer (no ## Media in text)",
        appended,
    )
    return "\n".join(lines) + "\n"


def _iter_eligible_sources(registry: "SourceRegistry"):
    """Yield (source, first_renderable_attachment) pairs.

    Only `media`-kind sources are eligible. A `media`-kind source is only
    ever registered by the `search_media_references` tool, so its presence
    is a reliable signal that the user intended to retrieve media (not an
    incidental attachment on a text fact). This prevents the gallery from
    appearing on unrelated questions whose facts happened to carry URLs.
    """
    seen_urls: set[str] = set()
    for source in registry._sources.values():  # noqa: SLF001
        if source.kind != "media":
            continue
        attachment = _pick_attachment(source)
        if attachment is None:
            continue
        if attachment.url in seen_urls:
            continue
        seen_urls.add(attachment.url)
        yield source, attachment


def _pick_attachment(source: "Source") -> "MediaAttachment | None":
    for a in source.attachments:
        if a.kind in _RENDERABLE_KINDS and a.url:
            return a
    return None


def _caption_for(source: "Source", attachment: "MediaAttachment") -> str:
    for candidate in (
        attachment.filename,
        attachment.title,
        attachment.alt_text,
        source.title,
    ):
        if candidate and candidate.strip():
            return candidate.strip()
    return attachment.kind or "attachment"


def _context_for(source: "Source") -> str:
    excerpt = (source.excerpt or "").strip().replace("\n", " ")
    if len(excerpt) > 80:
        cutoff = excerpt[:80]
        ws = cutoff.rfind(" ")
        if ws >= 48:
            cutoff = cutoff[:ws]
        excerpt = cutoff.rstrip() + "…"
    return excerpt or source.title or "channel media"
