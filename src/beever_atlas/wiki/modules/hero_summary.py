"""``hero_summary`` module — frontend renderer.

Renders the page's bold TL;DR + 2-3 sentence summary + a compact stat
strip showing the highlight counts (critical / decision / open
question / tension). Always module #1 when ``fact_count >= 1``.

The orchestrator populates this module's data from the existing
``tldr`` + ``overview`` fields the planner LLM returns (no
additional LLM call). The highlights counts come from
``compute_signals`` so they stay in sync with the validator's
view of the topic.

This file is purely a builder — there is no Python ``render()``
because the renderer lives in
``web/src/components/wiki/modules/HeroSummaryModule.tsx``.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers


def _critical_count_from_facts(facts: list[Any] | None) -> int:
    """Count facts whose importance normalises to ``"critical"``.

    Mirrors the threshold used by ``key_facts.build_key_facts_data``
    (numeric ≥9 OR explicit string ``"critical"``). Numeric inputs
    of NaN / non-numeric fall back to non-critical.
    """
    if not isinstance(facts, list):
        return 0
    n = 0
    for f in facts:
        if not isinstance(f, dict):
            continue
        v = f.get("importance")
        if isinstance(v, (int, float)):
            try:
                if float(v) >= 9:
                    n += 1
                continue
            except (TypeError, ValueError):
                continue
        if isinstance(v, str) and v.strip().lower() == "critical":
            n += 1
    return n


def build_hero_summary_data(
    *,
    tldr: str,
    overview: str,
    signals: dict[str, Any],
    facts: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the structured payload the frontend HeroSummaryModule
    consumes.

    Inputs come from the orchestrator's existing pipeline:
    ``tldr`` and ``overview`` are the planner LLM's outputs;
    ``signals`` is the dict ``compute_signals`` produces; ``facts``
    is the same per-module fact list ``key_facts`` consumes (used
    here only to count critical facts).

    Returns a dict matching the spec shape:
        {
          "label": "Summary",
          "renderer_kind": "frontend",
          "tldr": "<bold sentence>",
          "summary": "<2-3 sentence overview>",
          "highlights": {
            "critical_count": int,
            "decision_count": int,
            "open_question_count": int,
            "tension_count": int,
          }
        }
    """
    sig = signals or {}
    critical_count = _critical_count_from_facts(facts)
    decision_count = int(sig.get("decision_count", 0) or 0)
    open_question_count = int(sig.get("open_question_count", 0) or 0)
    # Tensions don't have a first-class signal yet — derive a simple
    # heuristic from the conflict_count signal when present, otherwise
    # zero. The frontend skips the chip when count is 0 so this is a
    # safe default.
    tension_count = int(sig.get("conflict_count", 0) or 0)

    return {
        "label": "Summary",
        "renderer_kind": "frontend",
        # Strip safety markers in case the LLM echoed fact text into
        # the tldr / overview verbatim — wrappers must never reach
        # the frontend.
        "tldr": _strip_safety_markers(tldr),
        "summary": _strip_safety_markers(overview),
        "highlights": {
            "critical_count": int(critical_count),
            "decision_count": int(decision_count),
            "open_question_count": int(open_question_count),
            "tension_count": int(tension_count),
        },
    }
