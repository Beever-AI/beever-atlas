"""``timeline`` module — ordered events with dates and citations.

Renders as a markdown bullet list ``**YYYY-MM-DD** — event text [N]``
sorted ascending by date. Robust to missing dates (those entries
sort last with a placeholder).
"""

from __future__ import annotations

from typing import Any


def _date_key(entry: dict[str, Any]) -> str:
    """Sort key — entries without a date sort last via the high
    sentinel "9999". Returning the raw string is safe because all
    real dates are ISO-prefixed (YYYY-MM-DD)."""
    d = (entry.get("date") or "").strip()
    return d if d else "9999"


def render(data: dict[str, Any]) -> str:
    events = data.get("events") or []
    if not isinstance(events, list) or not events:
        return ""

    rows = sorted([e for e in events if isinstance(e, dict)], key=_date_key)
    lines: list[str] = []
    for e in rows:
        date = (e.get("date") or "").strip()
        text = (e.get("event") or e.get("text") or "").strip()
        if not text:
            continue
        cite = (e.get("citations") or "").strip()
        date_part = f"**{date}** — " if date else ""
        cite_part = f" {cite}" if cite else ""
        lines.append(f"- {date_part}{text}{cite_part}")
    return "\n".join(lines)
