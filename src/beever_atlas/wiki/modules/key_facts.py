"""``key_facts`` module — frontend renderer (v2).

History: v1 was a python renderer that produced a GFM table via
``render_key_facts_table``. v2 swaps to a frontend renderer so the
React component can group by fact_type, color severity, and lazy-
expand long lists. The python ``render()`` is kept as a thin
fallback used by the catastrophic-fallback path in the orchestrator
(``_fallback_output``) and as a legacy callable. The orchestrator's
hot path now calls ``build_key_facts_data()`` to produce the
structured JSON the frontend consumes.

Note: ``render_key_facts_table()`` (in ``wiki/render.py``) is still
used by ``_assemble_resources_markdown`` for the Resources page —
do not remove it.
"""

from __future__ import annotations

import re
from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers
from beever_atlas.wiki.render import render_key_facts_table


# First-sentence detection — matches up to the first sentence terminator
# (period/question mark/exclamation) followed by whitespace OR end-of-
# string. Falls back to the whole text when no terminator is found.
_FIRST_SENTENCE_RE = re.compile(r"^(.*?[.!?])(?:\s|$)")
# How long a "title" snippet can be before we hard-truncate. Spec: ~140
# chars. We pick at a word boundary when possible.
_TITLE_MAX_CHARS = 140


def _first_sentence(text: str) -> str:
    """Return the first sentence of ``text`` truncated to ~140 chars.

    The truncation cuts at a word boundary when one falls in the second
    half of the budget, otherwise at the budget's end with an ellipsis.
    Empty / whitespace-only input returns ``""``.
    """
    if not text:
        return ""
    s = " ".join(str(text).split())
    if not s:
        return ""
    m = _FIRST_SENTENCE_RE.match(s)
    candidate = m.group(1) if m else s
    if len(candidate) <= _TITLE_MAX_CHARS:
        return candidate
    # Cut at a word boundary if one falls past the half-way mark, else
    # hard-truncate with an ellipsis.
    budget = candidate[:_TITLE_MAX_CHARS]
    last_space = budget.rfind(" ")
    if last_space >= _TITLE_MAX_CHARS // 2:
        return budget[:last_space].rstrip(" ,;:") + "…"
    return budget.rstrip() + "…"


def _normalize_importance(value: Any) -> str:
    """Coerce mixed importance representations to one of the four
    severity buckets the frontend understands: ``critical | high |
    medium | low``. Numeric inputs map by threshold (≥9 critical, ≥7
    high, ≥4 medium, else low). String inputs are lowercased and
    matched directly when valid; otherwise default to ``medium``.
    """
    if value is None:
        return "medium"
    if isinstance(value, (int, float)):
        v = float(value)
        if v >= 9:
            return "critical"
        if v >= 7:
            return "high"
        if v >= 4:
            return "medium"
        return "low"
    s = str(value).strip().lower()
    if s in {"critical", "high", "medium", "low"}:
        return s
    # Heuristic for textual numbers like "8" or "high-priority"
    try:
        return _normalize_importance(float(s))
    except (TypeError, ValueError):
        pass
    if "crit" in s:
        return "critical"
    if "high" in s:
        return "high"
    if "low" in s:
        return "low"
    return "medium"


def _normalize_fact_type(value: Any) -> str:
    """Lowercase + snake-case the fact_type so frontend grouping is
    deterministic. Empty or unknown returns ``"observation"`` as the
    catch-all bucket."""
    if value is None:
        return "observation"
    s = str(value).strip().lower().replace("-", "_").replace(" ", "_")
    return s or "observation"


# Default group ordering for the frontend. Matches the spec.
_GROUPS_ORDER: tuple[str, ...] = (
    "decision",
    "observation",
    "open_question",
    "action_item",
    "opinion",
)


def build_key_facts_data(facts: list[dict] | None) -> dict[str, Any]:
    """Build the structured JSON payload the frontend KeyFactsModule
    consumes. Pure function over ``facts`` — no IO, no LLM. Sorted by
    importance DESC, then date DESC. Each item carries the fields the
    React component needs (title, body, fact_type, importance, author,
    ts, source, citations).
    """
    if not isinstance(facts, list):
        return {
            "label": "Key Facts",
            "renderer_kind": "frontend",
            "items": [],
            "groups": list(_GROUPS_ORDER),
        }

    items: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        body = f.get("memory_text") or f.get("fact") or f.get("text") or ""
        # Strip <untrusted>/<sanitized>/<external> wrappers before the
        # text crosses to the frontend. The orchestrator wraps fact
        # text for LLM-context defense; the wrappers must NOT survive
        # to the user-facing card.
        body = _strip_safety_markers(body)
        title = _first_sentence(body)
        importance = _normalize_importance(f.get("importance"))
        fact_type = _normalize_fact_type(f.get("fact_type") or f.get("type"))
        author_name = f.get("author_name") or f.get("user_name") or f.get("author") or ""
        author_id = f.get("author_id") or f.get("user_id") or ""
        ts = f.get("message_ts") or f.get("timestamp") or f.get("date") or ""
        source_url = f.get("permalink") or f.get("source_url") or ""
        platform = f.get("platform") or ""
        citations = f.get("citations") or []
        if not isinstance(citations, list):
            citations = []
        items.append(
            {
                "fact_id": str(f.get("fact_id") or f.get("id") or ""),
                "title": title or body[:_TITLE_MAX_CHARS],
                "body": body,
                "fact_type": fact_type,
                "importance": importance,
                "author": {
                    "name": str(author_name),
                    "id": str(author_id),
                },
                "ts": str(ts),
                "source": {
                    "url": str(source_url),
                    "platform": str(platform),
                },
                "citations": citations,
            }
        )

    # Sort: importance DESC, then date DESC. Map severity buckets to a
    # numeric score so the sort is total even with mixed inputs.
    sev_rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    items.sort(
        key=lambda it: (
            sev_rank.get(it["importance"], 0),
            it.get("ts") or "",
        ),
        reverse=True,
    )

    return {
        "label": "Key Facts",
        "renderer_kind": "frontend",
        "items": items,
        "groups": list(_GROUPS_ORDER),
    }


def render(data: dict[str, Any]) -> str:
    """Legacy renderer — emits the GFM markdown table.

    Kept as a fallback for the orchestrator's catastrophic-fallback
    path (``_fallback_output``) and for any caller that imports
    ``key_facts.render`` directly. Prefer ``build_key_facts_data``
    for the new frontend pipeline.
    """
    facts = data.get("facts") or []
    if not isinstance(facts, list):
        return ""
    max_rows = int(data.get("max_rows", 8))
    return render_key_facts_table(facts, max_rows=max_rows)
