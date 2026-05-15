"""``flow_chart`` module — mermaid ``graph LR`` of process steps.

Input: ``{"steps": [{"id": str, "label": str}, ...], "edges": [
    {"from": id, "to": id, "label": str (optional)}, ...]}``

Emits a fenced ```mermaid block. Empty steps → empty string.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.modules._mermaid import safe_id, safe_label


def render(data: dict[str, Any]) -> str:
    steps = data.get("steps") or []
    edges = data.get("edges") or []
    if not isinstance(steps, list) or not steps:
        return ""

    # Build ID map so callers can pass arbitrary strings as IDs.
    id_map: dict[str, str] = {}
    lines = ["```mermaid", "graph LR"]
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        raw_id = step.get("id") or step.get("label") or f"step_{i}"
        sid = safe_id(str(raw_id), fallback=f"S{i}")
        id_map[str(raw_id)] = sid
        label = safe_label(str(step.get("label") or raw_id))
        lines.append(f"    {sid}[{label}]")

    for edge in edges:
        if not isinstance(edge, dict):
            continue
        src_raw = str(edge.get("from") or "")
        dst_raw = str(edge.get("to") or "")
        if src_raw not in id_map or dst_raw not in id_map:
            continue
        src = id_map[src_raw]
        dst = id_map[dst_raw]
        label = safe_label(str(edge.get("label") or ""))
        if label:
            lines.append(f"    {src} -->|{label}| {dst}")
        else:
            lines.append(f"    {src} --> {dst}")

    lines.append("```")
    return "\n".join(lines)
