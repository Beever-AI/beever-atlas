"""``related_threads`` module — capped 5-item list of related topics.

Format: ``- **[Title](/wiki/<slug>)** — one-line "why related" reason``

The 5-item cap is enforced here (not just in the planner) so even a
buggy planner that emits 30 related items still renders 5. The
"why related" reason is what makes this useful — a bare list of
links is the failure mode that triggered this redesign.
"""

from __future__ import annotations

from typing import Any

_MAX_ITEMS = 5


def render(data: dict[str, Any]) -> str:
    related = data.get("related") or []
    if not isinstance(related, list) or not related:
        return ""

    lines: list[str] = []
    for r in related[:_MAX_ITEMS]:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "").strip()
        slug = (r.get("slug") or "").strip()
        reason = (r.get("reason") or "").strip()
        if not title:
            continue
        link = f"[{title}](/wiki/{slug})" if slug else title
        if reason:
            lines.append(f"- **{link}** — {reason}")
        else:
            lines.append(f"- **{link}**")
    return "\n".join(lines)
