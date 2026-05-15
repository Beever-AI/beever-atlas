"""``folder_stats`` module — frontend renderer.

4-card stat strip for folder index pages. Aggregates the descendant
pages' fact lists into headline counts: total memories, decisions,
open questions, and distinct contributors. The cards replace the
"Themes & threads" prose blob with at-a-glance numbers — readers
grasp the folder's scale before scrolling into prose.

Pure builder — the React renderer lives in
``web/src/components/wiki/modules/FolderStatsModule.tsx``.

Eligibility (catalog predicate):
  - ``signals["archetype"] == "folder"``
  - ``signals["child_count"] >= 2``

The catalog predicate gates the planner; the builder itself is
defensive against non-folder usage (returns an empty stat list when
``descendants`` is empty).
"""

from __future__ import annotations

from typing import Any


def _fact_type(fact: Any) -> str:
    if not isinstance(fact, dict):
        return ""
    return str(fact.get("fact_type") or "").strip().lower()


def _author_name(fact: Any) -> str:
    if not isinstance(fact, dict):
        return ""
    return str(fact.get("author_name") or fact.get("user_name") or fact.get("author") or "").strip()


def build_folder_stats_data(descendants: list[dict] | None) -> dict[str, Any]:
    """Aggregate descendant pages into headline stat cards.

    ``descendants`` is a list of page-shaped dicts with at minimum
    ``facts`` (a list of fact dicts). Each fact contributes one
    "memory"; facts whose ``fact_type`` normalises to ``"decision"``
    or ``"question"`` add to the respective counts; distinct
    ``author_name`` values across all facts feed the contributors
    count.

    Returns:
        {
          "label": "Folder stats",
          "renderer_kind": "frontend",
          "stats": [
            {"value": "<int as str>", "label": "memories"},
            {"value": "<int as str>", "label": "decisions"},
            {"value": "<int as str>", "label": "open questions"},
            {"value": "<int as str>", "label": "contributors"},
          ],
          "subpage_count": <int>
        }
    """
    if not isinstance(descendants, list):
        descendants = []

    total_memories = 0
    total_decisions = 0
    total_questions = 0
    distinct_contributors: set[str] = set()

    for d in descendants:
        if not isinstance(d, dict):
            continue
        facts = d.get("facts") or []
        if not isinstance(facts, list):
            continue
        total_memories += len(facts)
        for f in facts:
            ft = _fact_type(f)
            if ft == "decision":
                total_decisions += 1
            elif ft == "question":
                total_questions += 1
            name = _author_name(f)
            if name:
                distinct_contributors.add(name)

    return {
        "label": "Folder stats",
        "renderer_kind": "frontend",
        "stats": [
            {"value": str(total_memories), "label": "memories"},
            {"value": str(total_decisions), "label": "decisions"},
            {"value": str(total_questions), "label": "open questions"},
            {"value": str(len(distinct_contributors)), "label": "contributors"},
        ],
        "subpage_count": len(descendants),
    }
