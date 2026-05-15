"""``entity_diagram`` module — mermaid ``graph TD`` of entity-to-entity
relationships within a topic.

Input: ``{"entities": [{"id": str, "label": str, "kind": str (optional)},
...], "relationships": [{"from": id, "to": id, "label": str (optional)},
...]}``

Skips when there are fewer than 3 entities OR fewer than 5 edges
(matches the spec eligibility rule).
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.modules._mermaid import safe_id, safe_label


def render(data: dict[str, Any]) -> str:
    entities = data.get("entities") or []
    relationships = data.get("relationships") or []
    if not isinstance(entities, list) or not isinstance(relationships, list):
        return ""
    if len(entities) < 3 or len(relationships) < 5:
        return ""

    id_map: dict[str, str] = {}
    lines = ["```mermaid", "graph TD"]
    for i, ent in enumerate(entities):
        if not isinstance(ent, dict):
            continue
        raw_id = ent.get("id") or ent.get("label") or f"e_{i}"
        eid = safe_id(str(raw_id), fallback=f"E{i}")
        id_map[str(raw_id)] = eid
        label = safe_label(str(ent.get("label") or raw_id))
        kind = (ent.get("kind") or "").strip().lower()
        # Hint the kind via shape: people in rounded boxes, others in
        # default rectangles. Keeps the diagram visually scannable.
        if kind in {"person", "people", "user", "contributor"}:
            lines.append(f"    {eid}({label})")
        else:
            lines.append(f"    {eid}[{label}]")

    for rel in relationships:
        if not isinstance(rel, dict):
            continue
        src_raw = str(rel.get("from") or "")
        dst_raw = str(rel.get("to") or "")
        if src_raw not in id_map or dst_raw not in id_map:
            continue
        src = id_map[src_raw]
        dst = id_map[dst_raw]
        label = safe_label(str(rel.get("label") or ""))
        if label:
            lines.append(f"    {src} -->|{label}| {dst}")
        else:
            lines.append(f"    {src} --> {dst}")

    lines.append("```")
    return "\n".join(lines)
