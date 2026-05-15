"""Render persisted ``narrative_sections`` to Markdown for export.

The narrative article is the new spotlight: it lives on
``WikiPage.narrative_sections`` (a structured field), NOT on
``WikiPage.content`` (the markdown body). Without this helper, the
``GET /api/channels/{channel_id}/wiki/download`` route emits only the
legacy module-substituted body and silently drops the article.

This helper inline-renders the structured payload into Markdown so the
exported file matches what the user reads on the rendered wiki page.

Contract:
- Pure function over the persisted ``narrative_sections`` payload.
  No IO, no LLM, no global state.
- Backward compat: pages without narrative_sections produce an empty
  string so the caller can prepend safely.
- ``[f_xxx]`` patterns inside paragraph text become ``[N]`` superscript
  references where ``N`` is the 1-indexed display number from a per-
  article fact_id → number map (built in order of first occurrence).
- agent-inference paragraphs prepend ``*[agent-inference]*``.
- Visual kinds map to their canonical Markdown form. Code stays
  verbatim (no citation parsing — same exemption as the React renderer).
"""

from __future__ import annotations

import re
from typing import Any

# Mirrors the inline-citation regex in the React renderer
# (``NarrativeArticleModule.tsx`` ``INLINE_CITATION_RE``). Keeping the
# two regexes structurally identical means Markdown export and live
# render agree on what is a citation marker vs. literal text.
_INLINE_CITATION_RE = re.compile(r"\[(f_[a-zA-Z0-9_]+(?:\s*,\s*f_[a-zA-Z0-9_]+)*)\]")


def _build_fact_id_index(sections: list[dict[str, Any]]) -> dict[str, int]:
    """Build fact_id → 1-indexed display number from first-occurrence order.

    Mirrors the frontend's ``buildFactIdIndex`` so exported markdown shows
    the same citation numbers the user saw on the rendered page.
    """
    idx: dict[str, int] = {}
    for section in sections:
        paragraphs = section.get("paragraphs") or []
        if not isinstance(paragraphs, list):
            continue
        for p in paragraphs:
            if not isinstance(p, dict):
                continue
            citations = p.get("citations") or []
            if not isinstance(citations, list):
                continue
            for cid in citations:
                if not isinstance(cid, str) or not cid:
                    continue
                if cid not in idx:
                    idx[cid] = len(idx) + 1
    return idx


def _rewrite_inline_citations(text: str, fact_id_index: dict[str, int]) -> str:
    """Replace ``[f_xxx]`` / ``[f_xxx, f_yyy]`` markers with ``[N]`` /
    ``[N, M]`` references using the per-article display index.

    A fact_id NOT in the index falls through to its raw form so the
    export does not silently drop a citation we cannot number — keeps
    the data discoverable in the exported file.
    """

    def _sub(match: re.Match[str]) -> str:
        chain = match.group(1)
        ids = [s.strip() for s in chain.split(",") if s.strip()]
        nums: list[str] = []
        for fid in ids:
            n = fact_id_index.get(fid)
            if n is None:
                # Fall through unchanged — preserves the raw fact_id so
                # the export is round-trippable into a future render.
                nums.append(fid)
            else:
                nums.append(str(n))
        return f"[{', '.join(nums)}]"

    return _INLINE_CITATION_RE.sub(_sub, text)


def _render_visual(visual: dict[str, Any]) -> str:
    """Render one ``visual`` payload as Markdown. Returns ``""`` for
    unknown / malformed shapes so the caller can skip cleanly.
    """
    kind = str(visual.get("kind") or "").strip().lower()
    content = visual.get("content")

    if kind == "table":
        if not isinstance(content, dict):
            return ""
        headers_raw = content.get("headers") or []
        rows_raw = content.get("rows") or []
        if not isinstance(headers_raw, list) or not isinstance(rows_raw, list):
            return ""
        headers = [str(h) for h in headers_raw]
        rows: list[list[str]] = []
        for row in rows_raw:
            if isinstance(row, list):
                rows.append([str(c) for c in row])
        if not headers and not rows:
            return ""
        # GFM table — header row, separator row, then data rows.
        out: list[str] = []
        if headers:
            out.append("| " + " | ".join(headers) + " |")
            out.append("|" + "|".join([" --- "] * len(headers)) + "|")
        for r in rows:
            # Pad short rows so the column count matches the header.
            padded = r + [""] * (len(headers) - len(r)) if headers else r
            out.append("| " + " | ".join(padded) + " |")
        return "\n".join(out)

    if kind == "mermaid":
        if not isinstance(content, str) or not content.strip():
            return ""
        return f"```mermaid\n{content.strip()}\n```"

    if kind == "list":
        if not isinstance(content, dict):
            return ""
        items_raw = content.get("items") or []
        if not isinstance(items_raw, list):
            return ""
        items = [str(i) for i in items_raw if i]
        if not items:
            return ""
        ordered = bool(content.get("ordered"))
        if ordered:
            return "\n".join(f"{i + 1}. {item}" for i, item in enumerate(items))
        return "\n".join(f"- {item}" for item in items)

    if kind == "callout":
        if not isinstance(content, dict):
            return ""
        variant_raw = str(content.get("variant") or content.get("type") or "note").strip().lower()
        text = content.get("text")
        if not isinstance(text, str):
            text = content.get("content") if isinstance(content.get("content"), str) else ""
        text = str(text or "").strip()
        if not text:
            return ""
        # Map to GFM alert / callout syntax. ``info`` falls back to
        # ``NOTE`` since the frontend's ``CalloutBox`` only knows the
        # NOTE / TIP / WARNING set.
        gfm_kind = {
            "tip": "TIP",
            "warning": "WARNING",
            "note": "NOTE",
            "info": "NOTE",
        }.get(variant_raw, "NOTE")
        # GFM callout: each body line prefixed with ``> ``. Multi-line
        # text is supported by quoting each line.
        body_lines = text.split("\n")
        return "> [!" + gfm_kind + "]\n" + "\n".join(f"> {ln}" for ln in body_lines)

    if kind == "code":
        # Code block — preserve verbatim. Accept both string content
        # and the ``{language, code}`` dict shape the v3 prompt emits.
        if isinstance(content, str):
            code = content
            language = ""
        elif isinstance(content, dict):
            code = content.get("code")
            if not isinstance(code, str):
                code = content.get("content") if isinstance(content.get("content"), str) else ""
            language_raw = content.get("language")
            language = language_raw if isinstance(language_raw, str) else ""
        else:
            return ""
        code = str(code or "")
        if not code.strip():
            return ""
        return f"```{language}\n{code}\n```"

    if kind == "blockquote":
        if isinstance(content, str):
            text = content
            attribution = ""
        elif isinstance(content, dict):
            text = content.get("content") or content.get("text") or ""
            attribution = content.get("attribution") or ""
            if not isinstance(text, str):
                text = ""
            if not isinstance(attribution, str):
                attribution = ""
        else:
            return ""
        text = str(text or "").strip()
        if not text:
            return ""
        body_lines = text.split("\n")
        # Use a distinct name from the table branch's ``out: list[str]``
        # so type-checkers don't flag the str/list mismatch across
        # mutually-exclusive code paths.
        quoted = "\n".join(f"> {ln}" for ln in body_lines)
        if attribution:
            quoted += f"\n> — {attribution}"
        return quoted

    return ""


def narrative_sections_to_markdown(
    sections: list[dict[str, Any]] | None,
    *,
    metadata_line: bool = True,
) -> str:
    """Render a list of narrative sections to Markdown.

    Returns ``""`` when the input is empty / malformed so the caller
    can prepend safely without conditional guards.

    The output starts with a metadata line (reading time + memories
    synthesised) when ``metadata_line=True`` so the exported markdown
    matches the live render's article header. Disable for round-trip
    or testing scenarios where the metadata is noise.

    Section heading level: ``##``. The page already opens at ``##``
    in the export route — narrative sections nest as siblings of the
    page heading, NOT as children of it, because each section is a
    distinct chunk of content. Callers that want article sub-nesting
    should down-level the markdown with a post-pass.
    """
    if not isinstance(sections, list) or not sections:
        return ""

    fact_id_index = _build_fact_id_index(sections)

    out_parts: list[str] = []

    if metadata_line:
        # Reading time = ceil(words / 200), min 1 when any words exist.
        # Mirrors the frontend ``readingTimeMinutes``.
        total_words = 0
        distinct_facts: set[str] = set()
        for section in sections:
            paragraphs = section.get("paragraphs") or []
            if not isinstance(paragraphs, list):
                continue
            for p in paragraphs:
                if not isinstance(p, dict):
                    continue
                text = p.get("text")
                if isinstance(text, str):
                    total_words += len(text.split())
                citations = p.get("citations") or []
                if isinstance(citations, list):
                    for c in citations:
                        if isinstance(c, str) and c:
                            distinct_facts.add(c)
        if total_words > 0 or distinct_facts:
            minutes = max(1, (total_words + 199) // 200) if total_words > 0 else 0
            badges: list[str] = []
            if minutes > 0:
                badges.append(f"{minutes} min read")
            if distinct_facts:
                noun = "memory" if len(distinct_facts) == 1 else "memories"
                badges.append(f"{len(distinct_facts)} {noun} synthesized")
            if badges:
                out_parts.append("_" + " · ".join(badges) + "_")

    for section in sections:
        if not isinstance(section, dict):
            continue
        heading = section.get("heading")
        if not isinstance(heading, str) or not heading.strip():
            continue
        out_parts.append(f"## {heading.strip()}")

        paragraphs = section.get("paragraphs") or []
        if isinstance(paragraphs, list):
            for p in paragraphs:
                if not isinstance(p, dict):
                    continue
                text = p.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                rendered = _rewrite_inline_citations(text.strip(), fact_id_index)
                if p.get("is_inference"):
                    rendered = "*[agent-inference]* " + rendered
                out_parts.append(rendered)

        visual = section.get("visual")
        if isinstance(visual, dict):
            visual_md = _render_visual(visual)
            if visual_md:
                out_parts.append(visual_md)

    # Use double-newlines so each piece is a distinct Markdown block.
    return "\n\n".join(out_parts).strip()


__all__ = ["narrative_sections_to_markdown"]
