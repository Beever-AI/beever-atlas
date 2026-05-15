"""``narrative_article`` module — frontend renderer.

Reads the persisted ``narrative_sections`` payload (produced by the v3
``MODULE_COMPILE_PROMPT`` and validated by ``narrative_validator``) and
returns the structured data the React ``NarrativeArticleModule``
component consumes directly.

Architecture: this module is the new spotlight. When it is present in
the plan, the frontend renders it FIRST (above existing modules), and
the existing 26 modules become a "Reference & Evidence" appendix below.
When it is absent (flag OFF, fallback fired, or pre-narrative page),
the page renders module-only (today's behavior, unchanged).

Design references:
- ``openspec/changes/wiki-narrative-articles/proposal.md``
- ``openspec/changes/wiki-narrative-articles/design.md`` (Decisions 4 + 5)

The builder is fail-safe: any malformed section payload is skipped
rather than raising. ``_strip_safety_markers`` is applied to every
string field that flows to the frontend so prompt-safety wrappers
never appear in user-facing card text.
"""

from __future__ import annotations

import re
from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

# ---------------------------------------------------------------------------
# Mermaid content sanitizer
# ---------------------------------------------------------------------------

# Matches a node token: identifier optionally followed by a bracket shape.
_MERMAID_NODE_TOKEN = r"[A-Za-z0-9_]+(?:\[[^\]]*\]|\([^)]*\)|\{[^}]*\})?"
# Matches an arrow token: pipe-style (-->|label|) or plain (-->).
_MERMAID_ARROW_TOKEN = r"-->?\|[^|]*\||-->"
# Combined chain pattern: three-or-more hop expression on a single line.
_MERMAID_CHAIN_RE = re.compile(
    rf"({_MERMAID_NODE_TOKEN})"
    rf"((?:\s*(?:{_MERMAID_ARROW_TOKEN})\s*{_MERMAID_NODE_TOKEN}){{2,}})"
)
_MERMAID_TOKEN_RE = re.compile(rf"({_MERMAID_NODE_TOKEN})|({_MERMAID_ARROW_TOKEN})")


def _clean_mermaid_content(content: str) -> str:
    """Sanitize raw LLM-generated mermaid source before it reaches the frontend.

    Fixes applied:
    - Unicode → ASCII: ``…`` → ``...``, smart quotes, en/em dashes.
    - Chained arrows on one line (``A --> B --> C``) split into per-edge lines.
    - Parens inside ``[label]`` shapes removed.
    - Forbidden chars (``; ` " <>``) inside ``[label]`` and ``(label)`` cleaned.
    - Trailing whitespace stripped per line.
    """
    # --- 1. Unicode normalisation (before any regex that inspects labels) ---
    content = content.replace("…", "...")  # … → ...
    content = content.replace("“", '"').replace("”", '"')  # "" → "
    content = content.replace("‘", "'").replace("’", "'")  # '' → '
    content = content.replace("–", "-").replace("—", "-")  # – — → -

    # --- 2. Split chained arrows ---
    def _split_chain(m: re.Match[str]) -> str:
        # findall returns (node_group, arrow_group) tuples; pick whichever matched.
        tokens: list[str] = [
            node or arrow for node, arrow in _MERMAID_TOKEN_RE.findall(m.group(0)) if node or arrow
        ]
        if len(tokens) < 5:
            return m.group(0)
        lines: list[str] = []
        for i in range(0, len(tokens) - 2, 2):
            lines.append(f"{tokens[i]} {tokens[i + 1]} {tokens[i + 2]}")
        return "\n    ".join(lines)

    content = _MERMAID_CHAIN_RE.sub(_split_chain, content)

    # --- 3. Per-line label cleanup ---
    cleaned_lines: list[str] = []
    for line in content.split("\n"):
        # Remove parens inside [] labels: [foo(bar)baz] → [foobarbaz]
        line = re.sub(
            r"\[([^\]]*)\(([^)]*)\)([^\]]*)\]",
            lambda mo: f"[{mo.group(1)}{mo.group(2)}{mo.group(3)}]",
            line,
        )
        # Clean forbidden chars inside [] labels
        line = re.sub(
            r"\[([^\]]*)\]",
            lambda mo: "[" + re.sub(r'["`\';<>]', " ", mo.group(1)) + "]",
            line,
        )
        # Clean forbidden chars inside () shapes (only after an identifier)
        line = re.sub(
            r"\b([A-Za-z_]\w*)\(([^)]*)\)",
            lambda mo: mo.group(1) + "(" + re.sub(r'["`\';<>]', " ", mo.group(2)) + ")",
            line,
        )
        cleaned_lines.append(line.rstrip())

    return "\n".join(cleaned_lines)


def _clean_paragraph(paragraph: Any) -> dict[str, Any] | None:
    """Coerce one paragraph payload to the canonical shape.

    Returns ``None`` when the payload is missing required fields so
    the caller can drop it. The shape produced is exactly what the
    frontend renderer expects: ``{text, citations[], is_inference}``.
    """
    if not isinstance(paragraph, dict):
        return None
    text = _strip_safety_markers(paragraph.get("text") or "")
    if not text:
        return None
    citations_raw = paragraph.get("citations") or []
    if not isinstance(citations_raw, list):
        citations_raw = []
    citations = [str(c) for c in citations_raw if c]
    is_inference = bool(paragraph.get("is_inference"))
    return {
        "text": text,
        "citations": citations,
        "is_inference": is_inference,
    }


def _clean_visual(visual: Any) -> dict[str, Any] | None:
    """Coerce a visual payload to the canonical shape.

    Returns ``None`` when the visual is missing or malformed (kind not
    in the allowed set). Allowed kinds: table, mermaid, list, callout,
    code, blockquote.
    """
    if not isinstance(visual, dict):
        return None
    kind = str(visual.get("kind") or "").strip().lower()
    if kind not in {"table", "mermaid", "list", "callout", "code", "blockquote"}:
        return None
    content = visual.get("content")
    # The frontend dispatches on ``kind`` and reads ``content`` shape-
    # appropriately (table → {headers, rows}; mermaid → str; etc.).
    # Strip safety markers from any string content; pass dict / list
    # content through untouched (the inner-string scrub happens at
    # render time, not at build time, to keep this O(1) per visual).
    if isinstance(content, str):
        content = _strip_safety_markers(content)
        if kind == "mermaid":
            content = _clean_mermaid_content(content)
    return {"kind": kind, "content": content}


def _clean_section(section: Any) -> dict[str, Any] | None:
    """Coerce one section payload to the canonical shape.

    Returns ``None`` when the section has no anchor or no paragraphs
    after cleaning so the caller drops it.
    """
    if not isinstance(section, dict):
        return None
    anchor = str(section.get("anchor") or "").strip()
    heading = _strip_safety_markers(section.get("heading") or "")
    if not anchor or not heading:
        return None
    paragraphs_raw = section.get("paragraphs") or []
    if not isinstance(paragraphs_raw, list):
        paragraphs_raw = []
    paragraphs: list[dict[str, Any]] = []
    for p in paragraphs_raw:
        cleaned = _clean_paragraph(p)
        if cleaned is not None:
            paragraphs.append(cleaned)
    if not paragraphs:
        return None

    citations_raw = section.get("citations") or []
    if not isinstance(citations_raw, list):
        citations_raw = []
    section_citations = [str(c) for c in citations_raw if c]
    # If the section's union-citations list is empty, derive it from
    # the per-paragraph citations so downstream consumers have a
    # consistent flat list to render the citation chips.
    if not section_citations:
        seen: set[str] = set()
        for p in paragraphs:
            for c in p.get("citations") or []:
                if c and c not in seen:
                    seen.add(c)
                    section_citations.append(c)

    visual = _clean_visual(section.get("visual"))

    coverage_raw = section.get("citation_coverage")
    if isinstance(coverage_raw, (int, float)):
        coverage = float(coverage_raw)
    else:
        # Recompute defensively when missing — paragraphs with at
        # least one citation / total paragraphs.
        with_citations = sum(1 for p in paragraphs if p.get("citations"))
        coverage = (with_citations / len(paragraphs)) if paragraphs else 0.0

    return {
        "anchor": anchor,
        "heading": heading,
        "paragraphs": paragraphs,
        "citations": section_citations,
        "visual": visual,
        "citation_coverage": coverage,
    }


def build_narrative_article_data(
    narrative_sections: list[dict] | None,
    facts: list[dict] | None = None,
) -> dict[str, Any]:
    """Build the structured JSON payload the frontend
    ``NarrativeArticleModule`` consumes.

    Pure function over the persisted sections — no IO, no LLM. Returns
    ``{label, renderer_kind, sections, total_words, distinct_facts_cited}``.

    Empty / malformed input returns a payload with an empty sections
    list; the frontend renders nothing in that case (the page falls
    back to module-only layout).

    ``facts`` is currently unused but accepted as a future hook for
    embedding fact previews directly in the payload (today the frontend
    fetches them via the citation popover instead — see
    ``CitationLink``).
    """
    sections_in = narrative_sections if isinstance(narrative_sections, list) else []
    sections: list[dict[str, Any]] = []
    total_words = 0
    distinct_facts: set[str] = set()
    for raw in sections_in:
        cleaned = _clean_section(raw)
        if cleaned is None:
            continue
        sections.append(cleaned)
        for p in cleaned.get("paragraphs") or []:
            text = p.get("text") or ""
            total_words += len(text.split())
        for cid in cleaned.get("citations") or []:
            if cid:
                distinct_facts.add(str(cid))

    return {
        "label": "Article",
        "renderer_kind": "frontend",
        "sections": sections,
        "total_words": total_words,
        "distinct_facts_cited": len(distinct_facts),
    }


__all__ = ["build_narrative_article_data"]
