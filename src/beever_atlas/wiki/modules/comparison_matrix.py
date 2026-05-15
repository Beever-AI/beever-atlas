"""``comparison_matrix`` module — N alternatives × M criteria GFM table.

Input shape: ``{"alternatives": ["A", "B"], "criteria": [
    {"name": "Cost", "values": {"A": "$10/mo", "B": "$25/mo"}},
    {"name": "Latency", "values": {"A": "200ms", "B": "50ms"}},
]}``

One column per alternative, one row per criterion. Cells fall back
to a single space when no value is provided so the GFM column count
stays consistent.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.render import escape_gfm_cell


def render(data: dict[str, Any]) -> str:
    alts = data.get("alternatives") or []
    criteria = data.get("criteria") or []
    if not isinstance(alts, list) or not isinstance(criteria, list):
        return ""
    if len(alts) < 2 or not criteria:
        return ""

    header_cells = ["Criterion"] + [str(a) for a in alts]
    sep_cells = ["---"] * len(header_cells)
    lines = [
        "| " + " | ".join(header_cells) + " |",
        "|" + "|".join(["---"] * len(header_cells)) + "|",
    ]
    _ = sep_cells  # readability — sep_cells documents the intent
    for crit in criteria:
        if not isinstance(crit, dict):
            continue
        name = (crit.get("name") or crit.get("criterion") or "").strip()
        values = crit.get("values") or {}
        if not name:
            continue
        cells = [escape_gfm_cell(name)]
        for a in alts:
            v = values.get(a) if isinstance(values, dict) else None
            cells.append(escape_gfm_cell(str(v)) if v is not None else " ")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)
