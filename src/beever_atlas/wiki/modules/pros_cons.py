"""``pros_cons`` module — two-column GFM table.

Input: ``{"pros": [str|dict], "cons": [str|dict]}`` where each entry
is either a plain string or ``{"text": str, "citations": str}``. Rows
zip pros/cons by index; the longer list dictates row count, the
shorter list pads with empty cells.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.render import escape_gfm_cell


def _entry_text(e: Any) -> str:
    if isinstance(e, dict):
        text = (e.get("text") or "").strip()
        cite = (e.get("citations") or "").strip()
        return f"{text} {cite}".strip() if cite else text
    return str(e or "").strip()


def render(data: dict[str, Any]) -> str:
    pros = data.get("pros") or []
    cons = data.get("cons") or []
    if not isinstance(pros, list) or not isinstance(cons, list):
        return ""
    if not pros and not cons:
        return ""

    rows_n = max(len(pros), len(cons))
    lines = ["| Pros | Cons |", "|------|------|"]
    for i in range(rows_n):
        p = _entry_text(pros[i]) if i < len(pros) else ""
        c = _entry_text(cons[i]) if i < len(cons) else ""
        lines.append("| " + escape_gfm_cell(p) + " | " + escape_gfm_cell(c) + " |")
    return "\n".join(lines)
