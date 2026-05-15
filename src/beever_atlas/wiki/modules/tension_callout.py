"""``tension_callout`` module — frontend renderer.

Surfaces a single contradicting position pair as a yellow-bordered
callout near the top of the page. Activated when the heuristic
tension detector finds at least one opposing-sentiment pair sharing
an entity tag (see ``tension_detector.detect_tensions``).

The catalog only permits ONE ``tension_callout`` per page in v1 —
when multiple tensions exist, the builder picks the first detected.
The remaining tensions are still queryable via the ``get_tensions``
MCP tool which walks the persisted module data.

Renderer lives in
``web/src/components/wiki/modules/TensionCalloutModule.tsx`` —
this file is purely a builder.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.modules.tension_detector import detect_tensions


def build_tension_callout_data(
    facts: list[Any] | None,
) -> dict[str, Any]:
    """Build the payload the React TensionCalloutModule consumes.

    Pure function — no IO, no LLM. Runs the heuristic detector and
    returns the FIRST detected tension. When no tension is detected
    (predicate misfire, or detector found none), returns the empty
    payload so the React component renders ``null``.

    Returns:
        ``{
            "label": "Tension",
            "renderer_kind": "frontend",
            "title": "<headline>",
            "status": "open" | "blocked" | "deferred",
            "since": "YYYY-MM-DD",
            "positions": [{"author", "stance", "fact_id"}, ...],
            "tension_id": "t_<8-char-hash>"
        }``

    The ``tension_id`` is deterministic — the same pair of facts
    always produces the same id (sorted-id hash). Re-runs do not
    churn the persisted page data.
    """
    result = detect_tensions(facts if isinstance(facts, list) else [])
    tensions = result.get("tensions") or []
    if not tensions:
        return {
            "label": "Tension",
            "renderer_kind": "frontend",
            "title": "",
            "status": "open",
            "since": "",
            "positions": [],
            "tension_id": "",
        }
    t = tensions[0]
    return {
        "label": "Tension",
        "renderer_kind": "frontend",
        "title": t.get("title") or "",
        "status": t.get("status") or "open",
        "since": t.get("since") or "",
        "positions": list(t.get("positions") or []),
        "tension_id": t.get("tension_id") or "",
    }


__all__ = ["build_tension_callout_data"]
