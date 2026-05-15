"""Serialize ``page.modules[]`` to Markdown for the wiki export.

Each module's ``data`` shape mirrors the Python builder that produced
it (see the sibling ``*.py`` files in this package) and the React
renderer that consumes it (see
``web/src/components/wiki/modules/``).

Contract:
- Pure functions over plain dicts. No IO, no LLM, no global state.
- If a module's ``data`` is missing or empty the serializer returns
  ``""`` and the caller skips it silently.
- Modules that render pre-compiled markdown (``MarkdownModule`` on
  the frontend) just re-emit that markdown verbatim under their
  heading.
- ``narrative_article`` is intentionally skipped — that module is
  already rendered by ``narrative_sections_to_markdown`` in the
  export route.
- Unknown module IDs fall through to ``""`` so future modules added
  to the catalog don't break existing exports.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _str(v: Any, default: str = "") -> str:
    """Coerce *v* to a stripped string, returning *default* on failure."""
    if v is None:
        return default
    return str(v).strip()


def _label(data: dict[str, Any], fallback: str) -> str:
    """Return the human-readable label stored in *data*, or *fallback*."""
    return _str(data.get("label")) or fallback


# ---------------------------------------------------------------------------
# Per-module serializers  (each returns "" to signal "skip silently")
# ---------------------------------------------------------------------------


def _markdown_module(data: dict[str, Any], fallback_label: str) -> str:
    """Most modules store pre-rendered GFM in ``data.markdown``.

    Returns the rendered markdown under a ``##`` heading derived from
    ``data.label``. Returns ``""`` when the markdown payload is empty.
    """
    markdown = _str(data.get("markdown"))
    if not markdown:
        return ""
    label = _label(data, fallback_label)
    return f"## {label}\n\n{markdown}"


def _hero_summary(data: dict[str, Any]) -> str:
    tldr = _str(data.get("tldr")).lstrip("**").rstrip("**").strip()
    summary = _str(data.get("summary"))
    if not tldr and not summary:
        return ""
    parts: list[str] = []
    if tldr:
        parts.append(f"**{tldr}**")
    if summary:
        parts.append(summary)
    h = data.get("highlights") or {}
    chips: list[str] = []
    for key, icon in [
        ("critical_count", "⚡"),
        ("decision_count", "✅"),
        ("open_question_count", "❓"),
        ("tension_count", "⚠"),
    ]:
        count = h.get(key) or 0
        if count:
            chips.append(f"{icon} {count}")
    if chips:
        parts.append("  ".join(chips))
    return "\n\n".join(parts)


def _key_facts(data: dict[str, Any]) -> str:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return ""
    label = _label(data, "Key Facts")
    lines: list[str] = [f"## {label}\n"]
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _str(item.get("title"))
        if not title:
            continue
        importance = _str(item.get("importance"), "medium")
        fact_type = _str(item.get("fact_type"), "observation")
        author_name = ""
        author = item.get("author")
        if isinstance(author, dict):
            author_name = _str(author.get("name"))
        ts = _str(item.get("ts"))
        body = _str(item.get("body"))

        meta_parts: list[str] = []
        if importance:
            meta_parts.append(f"*{importance}*")
        if fact_type:
            meta_parts.append(fact_type)
        if author_name:
            meta_parts.append(f"@{author_name}")
        if ts:
            meta_parts.append(ts)
        meta = " · ".join(meta_parts)

        lines.append(f"- **{title}**")
        if meta:
            lines.append(f"  {meta}")
        if body and body != title:
            # Indent body under the bullet so it reads as continuation.
            for body_line in body.splitlines():
                lines.append(f"  {body_line}")
    return "\n".join(lines)


def _stat_strip(data: dict[str, Any]) -> str:
    stats = data.get("stats")
    if not isinstance(stats, list) or not stats:
        return ""
    label = _label(data, "Stats")
    lines: list[str] = [f"## {label}\n"]
    for stat in stats:
        if not isinstance(stat, dict):
            continue
        value = _str(stat.get("value"))
        stat_label = _str(stat.get("label"))
        if not value:
            continue
        if stat_label:
            lines.append(f"- **{value}** {stat_label}")
        else:
            lines.append(f"- **{value}**")
    period = data.get("period") or {}
    from_date = _str(period.get("from"))
    to_date = _str(period.get("to"))
    if from_date or to_date:
        period_str = " – ".join(d for d in [from_date, to_date] if d)
        lines.append(f"\n*Period: {period_str}*")
    return "\n".join(lines)


def _decision_banner(data: dict[str, Any]) -> str:
    decision = _str(data.get("decision"))
    if not decision:
        return ""
    body = _str(data.get("body"))
    decided_by = data.get("decided_by") or {}
    decided_by_name = _str(decided_by.get("name") if isinstance(decided_by, dict) else decided_by)
    decided_at = _str(data.get("decided_at"))
    rationale = _str(data.get("rationale"))
    alternatives = data.get("alternatives_rejected") or []
    consequences = data.get("consequences_open") or []
    fact_id = _str(data.get("fact_id"))
    source_url = _str(data.get("source_url"))

    parts: list[str] = [f"## Decision\n\n**{decision}**"]
    if body:
        parts.append(body)
    meta: list[str] = []
    if decided_by_name:
        meta.append(f"Decided by {decided_by_name}")
    if decided_at:
        meta.append(decided_at)
    if meta:
        parts.append(" · ".join(meta))
    if rationale:
        parts.append(f"**Because:** {rationale}")
    if isinstance(alternatives, list) and alternatives:
        parts.append("**Alternatives rejected:**")
        parts.extend(f"- {a}" for a in alternatives if isinstance(a, str) and a.strip())
    if isinstance(consequences, list) and consequences:
        parts.append("**Open consequences:**")
        parts.extend(f"- {c}" for c in consequences if isinstance(c, str) and c.strip())
    if fact_id:
        parts.append(f"*Cite as: {fact_id}*")
    if source_url:
        parts.append(f"[source]({source_url})")
    return "\n\n".join(parts)


def _tension_callout(data: dict[str, Any]) -> str:
    title = _str(data.get("title"))
    positions = data.get("positions") or []
    if not title or not isinstance(positions, list) or not positions:
        return ""
    status = _str(data.get("status"), "open")
    since = _str(data.get("since"))
    tension_id = _str(data.get("tension_id"))

    header_parts = [f"⚠ TENSION · {status.upper()}"]
    if since:
        header_parts.append(f"since {since}")
    parts: list[str] = [f"## {' · '.join(header_parts)}\n\n**{title}**"]
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        author = _str(pos.get("author"), "Unknown")
        stance = _str(pos.get("stance"))
        fact_id = _str(pos.get("fact_id"))
        pos_line = f"- **{author}**"
        if stance:
            pos_line += f": {stance}"
        if fact_id:
            pos_line += f" *(cite {fact_id})*"
        parts.append(pos_line)
    if tension_id:
        parts.append(f"\n*Tension ID: {tension_id}*")
    return "\n\n".join(parts)


def _cross_cutting_decisions(data: dict[str, Any]) -> str:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return ""
    label = _label(data, "Cross-cutting Decisions")
    lines: list[str] = [f"## {label}\n"]
    for item in items:
        if not isinstance(item, dict):
            continue
        title = _str(item.get("title"))
        if not title:
            continue
        decided_by = _str(item.get("decided_by"))
        decided_at = _str(item.get("decided_at"))
        importance = _str(item.get("importance"), "medium")
        source_page = item.get("source_page") or {}
        source_title = _str(
            source_page.get("title") if isinstance(source_page, dict) else source_page
        )

        meta: list[str] = [f"*{importance}*"]
        if decided_by:
            meta.append(f"by {decided_by}")
        if decided_at:
            meta.append(decided_at)
        if source_title:
            meta.append(f"→ {source_title}")

        lines.append(f"- **{title}**")
        lines.append(f"  {' · '.join(meta)}")
    return "\n".join(lines)


def _top_contributors(data: dict[str, Any]) -> str:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return ""
    label = _label(data, "Top Contributors")
    lines: list[str] = [f"## {label}\n"]
    for item in items:
        if not isinstance(item, dict):
            continue
        name = _str(item.get("name"))
        if not name:
            continue
        count = item.get("contribution_count") or 0
        top_pages = item.get("top_pages") or []
        top_page_title = ""
        if isinstance(top_pages, list) and top_pages:
            first = top_pages[0]
            if isinstance(first, dict):
                top_page_title = _str(first.get("title"))
        line = f"- **{name}** — {count} contribution{'s' if count != 1 else ''}"
        if top_page_title:
            line += f" (most active: {top_page_title})"
        lines.append(line)
    return "\n".join(lines)


def _acronym_legend(data: dict[str, Any]) -> str:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return ""
    label = _label(data, "Terms used on this page")
    lines: list[str] = [f"## {label}\n"]
    for item in items:
        if not isinstance(item, dict):
            continue
        term = _str(item.get("term"))
        definition = _str(item.get("definition"))
        if not term:
            continue
        if definition:
            lines.append(f"- **{term}**: {definition}")
        else:
            lines.append(f"- **{term}**")
    return "\n".join(lines)


def _folder_stats(data: dict[str, Any]) -> str:
    """Folder stats module — delegate to the stored markdown when present."""
    return _markdown_module(data, "Folder Stats")


def _provenance_drawer(data: dict[str, Any]) -> str:
    """Provenance drawer — front-end only; fall back to stored markdown."""
    return _markdown_module(data, "Sources")


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_SERIALIZERS: dict[str, Any] = {
    # narrative_article is skipped — handled by narrative_sections_to_markdown
    "hero_summary": lambda d: _hero_summary(d),
    "key_facts": lambda d: _key_facts(d),
    "decision_log": lambda d: _markdown_module(d, "Decision Log"),
    "timeline": lambda d: _markdown_module(d, "Timeline"),
    "comparison_matrix": lambda d: _markdown_module(d, "Comparison"),
    "pros_cons": lambda d: _markdown_module(d, "Pros & Cons"),
    "quote_highlights": lambda d: _markdown_module(d, "Quote Highlights"),
    "flow_chart": lambda d: _markdown_module(d, "Flow Chart"),
    "entity_diagram": lambda d: _markdown_module(d, "Entity Diagram"),
    "open_questions": lambda d: _markdown_module(d, "Open Questions"),
    "subpage_cards": lambda d: _markdown_module(d, "Sub-pages"),
    "related_threads": lambda d: _markdown_module(d, "Related Threads"),
    "stat_strip": lambda d: _stat_strip(d),
    "decision_banner": lambda d: _decision_banner(d),
    "tension_callout": lambda d: _tension_callout(d),
    "cross_cutting_decisions": lambda d: _cross_cutting_decisions(d),
    "top_contributors": lambda d: _top_contributors(d),
    "folder_stats": lambda d: _folder_stats(d),
    "acronym_legend": lambda d: _acronym_legend(d),
    "provenance_drawer": lambda d: _provenance_drawer(d),
    # Media / embed modules — no meaningful text export; skip silently.
    "media_hero": lambda _: "",
    "media_inline": lambda _: "",
    "media_gallery": lambda _: "",
    "link_card": lambda _: "",
    "pdf_preview": lambda _: "",
    "video_embed": lambda _: "",
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def module_to_markdown(module: dict[str, Any]) -> str:
    """Serialize one module dict to Markdown.

    ``module`` must have at minimum ``{"id": "<module_id>", "data": {...}}``.
    Returns ``""`` when the module should be skipped (no data, narrative
    article, media embed, or unknown ID).
    """
    if not isinstance(module, dict):
        return ""
    module_id = _str(module.get("id"))
    if module_id == "narrative_article":
        # Already handled by narrative_sections_to_markdown.
        return ""
    data = module.get("data")
    if not isinstance(data, dict):
        return ""
    serializer = _SERIALIZERS.get(module_id)
    if serializer is None:
        # Unknown module ID — skip silently so future modules don't break
        # existing exports. Matches the frontend's ``dispatchModule`` default
        # which returns ``null`` for unknown IDs.
        return ""
    try:
        result = serializer(data)
        return result if isinstance(result, str) else ""
    except Exception:  # noqa: BLE001
        # Defensive — a malformed payload should never crash the export.
        return ""


def modules_to_markdown(
    modules: list[dict[str, Any]] | None,
    *,
    include_section_header: bool = True,
) -> str:
    """Serialize a full ``page.modules[]`` list to Markdown.

    Returns ``""`` when there are no non-empty modules to render.
    When *include_section_header* is True and at least one module has
    content, the output starts with a ``## Reference & Evidence``
    heading (matching the UI's collapsible appendix label) followed by
    each non-empty module block separated by ``\\n\\n``.

    The ``narrative_article`` module is always skipped here — callers
    should invoke ``narrative_sections_to_markdown`` for that content.
    """
    if not isinstance(modules, list) or not modules:
        return ""

    rendered: list[str] = []
    for mod in modules:
        md = module_to_markdown(mod)
        if md:
            rendered.append(md)

    if not rendered:
        return ""

    body = "\n\n".join(rendered)
    if include_section_header:
        return f"## Reference & Evidence\n\n{body}"
    return body


__all__ = ["module_to_markdown", "modules_to_markdown"]
