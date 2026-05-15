"""``decision_log`` module — GFM table of decisions with status badges.

Status badges: ✅ active, ❌ superseded, ⏳ pending. Falls through to
the raw status string for unknown statuses so the renderer never
swallows data.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.render import escape_gfm_cell

_STATUS_BADGES = {
    "active": "✅ active",
    "approved": "✅ active",
    "superseded": "❌ superseded",
    "rejected": "❌ rejected",
    "pending": "⏳ pending",
    "open": "⏳ pending",
}


def _badge(status: str) -> str:
    key = (status or "").strip().lower()
    return _STATUS_BADGES.get(key, status or "")


def render(data: dict[str, Any]) -> str:
    """Render a Decisions GFM table.

    ``data`` requires ``decisions: list[dict]`` where each entry has
    ``decision`` (text), ``status`` (str), ``made_by`` (str), and
    ``date`` (str). Empty list → empty string.
    """
    decisions = data.get("decisions") or []
    if not isinstance(decisions, list) or not decisions:
        return ""

    header = "| Decision | Status | Made by | Date |"
    sep = "|----------|--------|---------|------|"
    lines = [header, sep]
    for d in decisions:
        if not isinstance(d, dict):
            continue
        text = d.get("decision") or d.get("text") or ""
        status = d.get("status") or ""
        made_by = d.get("made_by") or d.get("author") or ""
        date = d.get("date") or ""
        lines.append(
            "| "
            + escape_gfm_cell(text)
            + " | "
            + escape_gfm_cell(_badge(status))
            + " | "
            + escape_gfm_cell(made_by)
            + " | "
            + escape_gfm_cell(date)
            + " |"
        )
    return "\n".join(lines)
