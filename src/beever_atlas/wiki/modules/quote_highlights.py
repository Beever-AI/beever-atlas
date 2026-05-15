"""``quote_highlights`` module — verbatim quotes as markdown blockquotes.

Each quote: ``> "text" — Author, YYYY-MM-DD [N]``. Verbatim preserves
authorial voice; attribution + date + citation gives the reader a
trail back to the source thread.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

# Strip newlines from quote bodies — multi-line quotes break the
# blockquote prefix on render and produce inconsistent visual blocks.
_NEWLINE_RE_REPLACEMENTS = (("\r\n", " "), ("\r", " "), ("\n", " "))


def _flatten(text: str) -> str:
    out = text
    for old, new in _NEWLINE_RE_REPLACEMENTS:
        out = out.replace(old, new)
    return out.strip()


def render(data: dict[str, Any]) -> str:
    quotes = data.get("quotes") or []
    if not isinstance(quotes, list) or not quotes:
        return ""

    lines: list[str] = []
    for q in quotes:
        if not isinstance(q, dict):
            continue
        text = _flatten(_strip_safety_markers(q.get("text") or ""))
        author = (q.get("author") or "").strip()
        date = (q.get("date") or "").strip()
        cite = (q.get("citations") or "").strip()
        if not text:
            continue
        attr_parts = [p for p in (author, date) if p]
        attribution = ", ".join(attr_parts)
        cite_part = f" {cite}" if cite else ""
        if attribution:
            lines.append(f'> "{text}" — {attribution}{cite_part}')
        else:
            lines.append(f'> "{text}"{cite_part}')
        lines.append("")  # blank line between quotes for blockquote separation
    # Trim trailing blank.
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)
