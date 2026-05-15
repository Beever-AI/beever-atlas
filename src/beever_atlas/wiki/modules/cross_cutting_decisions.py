"""``cross_cutting_decisions`` module — frontend renderer.

Surfaces the highest-importance decisions across a folder's
descendant pages as a vertical list. Each entry: the decision text
(first sentence, capitalized), who decided, when, and a small
"Source page →" link routing back to the descendant page that
carries it.

Replaces the prose-heavy "decisions across this folder" thread in
the legacy ``FOLDER_INDEX_PROMPT`` output. Pure builder — the
React renderer lives in
``web/src/components/wiki/modules/CrossCuttingDecisionsModule.tsx``.

Eligibility (catalog predicate):
  - ``signals["archetype"] == "folder"``
  - ``signals["descendant_decision_count"] >= 2``

Sort: importance DESC, then decided_at ASC (earliest decision wins
the slot when importance ties — first-decided usually has more
downstream context). Cap at 5 entries per strip.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers
from beever_atlas.wiki.modules.decision_banner import (
    _importance_score,
    _iso_date,
    _split_sentence,
)


def _first_sentence(text: str) -> str:
    """Capitalised first sentence of ``text`` (delegates to
    ``decision_banner._split_sentence``)."""
    head, _ = _split_sentence(text)
    return head


def _fact_type(fact: Any) -> str:
    if not isinstance(fact, dict):
        return ""
    return str(fact.get("fact_type") or "").strip().lower()


def _author_name(fact: Any) -> str:
    if not isinstance(fact, dict):
        return ""
    return str(fact.get("author_name") or fact.get("user_name") or fact.get("author") or "").strip()


def _importance_label(value: Any) -> str:
    """Return the importance bucket as a string label.

    Numeric scores normalise back to {critical/high/medium/low} so
    the frontend can colour-code consistently with ``key_facts``.
    """
    if isinstance(value, str) and value.strip():
        return value.strip().lower()
    score = _importance_score(value)
    if score >= 9:
        return "critical"
    if score >= 7:
        return "high"
    if score >= 4:
        return "medium"
    if score > 0:
        return "low"
    return "medium"


def build_cross_cutting_decisions_data(
    descendants: list[dict] | None,
    cap: int = 5,
) -> dict[str, Any]:
    """Build the payload the React CrossCuttingDecisionsModule consumes.

    Walks each descendant's ``facts`` list, filters to facts whose
    ``fact_type`` normalises to ``"decision"``, normalises the
    decision text (first sentence + safety-marker strip), and
    captures the source page so the frontend can render a deep-link
    back to it.

    Returns:
        {
          "label": "Cross-cutting decisions",
          "renderer_kind": "frontend",
          "items": [
            {
              "fact_id": "f_xyz",
              "title": "Adopt JWT for session auth.",
              "decided_by": "Jacky Chan",
              "decided_at": "2026-04-15",
              "importance": "high",
              "source_page": {"title": "...", "slug": "..."}
            },
            ...
          ]
        }
    """
    if not isinstance(descendants, list):
        descendants = []

    decisions: list[dict[str, Any]] = []
    for d in descendants:
        if not isinstance(d, dict):
            continue
        page_title = str(d.get("title") or "").strip()
        page_slug = str(d.get("slug") or "").strip()
        facts = d.get("facts") or []
        if not isinstance(facts, list):
            continue
        for f in facts:
            if _fact_type(f) != "decision":
                continue
            body = _strip_safety_markers(
                f.get("memory_text") or f.get("fact") or f.get("text") or ""
            )
            title = _first_sentence(body)
            if not title:
                continue
            fact_id = str(f.get("fact_id") or f.get("id") or "")
            decided_at = _iso_date(
                str(f.get("message_ts") or f.get("timestamp") or f.get("date") or "")
            )
            decisions.append(
                {
                    "fact_id": fact_id,
                    "title": title,
                    "decided_by": _author_name(f),
                    "decided_at": decided_at,
                    "importance": _importance_label(f.get("importance")),
                    "source_page": {
                        "title": page_title,
                        "slug": page_slug,
                    },
                }
            )

    # Sort: importance DESC (numeric score), then decided_at ASC
    # (earliest decision wins ties — first-mover decisions tend to
    # have more downstream context). Empty dates sort LAST so a
    # decision missing a timestamp doesn't crowd out dated peers.
    def _sort_key(item: dict[str, Any]) -> tuple[float, str]:
        score = -_importance_score(item.get("importance"))
        date = item.get("decided_at") or "￿"
        return (score, str(date))

    decisions.sort(key=_sort_key)
    decisions = decisions[: max(int(cap), 0)]

    return {
        "label": "Cross-cutting decisions",
        "renderer_kind": "frontend",
        "items": decisions,
    }
