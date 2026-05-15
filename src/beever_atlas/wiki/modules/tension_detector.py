"""Heuristic tension detection — pure function over a cluster's facts.

Tensions are detected at wiki compile time, NOT during extraction.
Reasons:
1. Detection needs the full cluster context (relative comparison).
2. Re-running over a cluster is cheap (no LLM call); re-doing
   extraction would re-extract every fact.
3. Operators who want to adjust detection thresholds don't need a
   re-extract pass — a wiki regen suffices.

The detector is deliberately conservative: false positives are more
visible than false negatives (a stray callout looks like a bug; a
missed tension is unnoticed). Tune via the constants below.
"""

from __future__ import annotations

import hashlib
from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

# Detection thresholds — tuned conservative.
_OPINION_TYPES = {"opinion", "decision", "recommendation"}

# (a, b) ordered pairs that count as contradicting. Membership test
# uses ``frozenset`` so order does not matter — both ``(a, b)`` and
# ``(b, a)`` collapse to the same key.
_OPPOSING_PAIRS: set[frozenset[str]] = {
    frozenset({"concerning", "positive"}),
    frozenset({"concerning", "recommendation"}),
}

_MIN_SHARED_ENTITY_OVERLAP = 1

# Title cap — keep tension callout headlines tight; longer text gets
# truncated with an ellipsis to avoid card-overflow on the frontend.
_MAX_TITLE_CHARS = 80


def _opposing_sentiments(a: str, b: str) -> bool:
    """Return True if two sentiment values are in the opposing set.

    Uses ``frozenset`` membership so the relationship is symmetric —
    no need to enumerate both ``(a, b)`` and ``(b, a)`` orderings.
    """
    if not a or not b:
        return False
    return frozenset({a, b}) in _OPPOSING_PAIRS


def _shared_entities(a: list[Any], b: list[Any]) -> set[str]:
    """Return the case-insensitive set of entity tags shared between
    two facts. Empty when either side has no entity_tags."""
    if not isinstance(a, list) or not isinstance(b, list):
        return set()
    sa = {str(t).strip().lower() for t in a if isinstance(t, str) and t.strip()}
    sb = {str(t).strip().lower() for t in b if isinstance(t, str) and t.strip()}
    return sa & sb


def _stable_tension_id(fact_a_id: str, fact_b_id: str) -> str:
    """Produce a deterministic ``t_<8-char-hash>`` id for the pair.

    The id is symmetric — sorted fact ids feed the hash so the same
    pair always produces the same id regardless of which fact is
    encountered first.
    """
    parts = sorted([str(fact_a_id), str(fact_b_id)])
    digest = hashlib.sha256(":".join(parts).encode("utf-8")).hexdigest()
    return f"t_{digest[:8]}"


def _first_sentence_capped(text: str, cap: int = _MAX_TITLE_CHARS) -> str:
    """Return the first sentence of ``text``, capped at ``cap`` chars.

    Strips safety markers up front. When the text exceeds ``cap``
    after the first-sentence cut, append ``"…"`` so readers see the
    truncation. Returns ``""`` for empty/whitespace input.
    """
    cleaned = _strip_safety_markers(text)
    if not cleaned:
        return ""
    flat = " ".join(cleaned.split())
    # First sentence boundary — terminator followed by space or end.
    first = flat
    for terminator in (".", "!", "?"):
        idx = flat.find(terminator)
        if idx > 0 and idx < len(first):
            first = flat[: idx + 1]
            break
    if len(first) <= cap:
        return first
    return first[: cap - 1].rstrip() + "…"


def _earlier_iso_date(a: str, b: str) -> str:
    """Return the lexicographically earlier of two ISO-prefix
    timestamps (YYYY-MM-DD or longer). Empty inputs are skipped.
    Returns ``""`` when both are empty."""
    aa = (a or "").strip()
    bb = (b or "").strip()
    if not aa:
        return bb[:10] if bb else ""
    if not bb:
        return aa[:10] if aa else ""
    chosen = aa if aa <= bb else bb
    return chosen[:10]


def detect_tensions(facts: list[dict] | None) -> dict[str, Any]:
    """Detect tension pairs across a cluster's facts.

    Pure function: same input → same output. ``tension_id`` is a
    stable hash of the contributing fact ids (sorted) so re-runs are
    deterministic — re-detecting the same pair always produces the
    same id.

    Returns:
        ``{"tensions": [...], "fact_annotations": {fact_id: {...}}}``

    A fact may participate in multiple tensions (rare). The
    ``fact_annotations`` map carries the FIRST tension found for each
    fact so the AtomicFact-level annotation is unambiguous; later
    tensions still get their own ``tension_id`` and appear in the
    returned ``tensions`` list, but don't overwrite the annotation.
    """
    out_tensions: list[dict[str, Any]] = []
    out_annotations: dict[str, dict[str, str]] = {}

    if not isinstance(facts, list):
        return {"tensions": out_tensions, "fact_annotations": out_annotations}

    # Filter to opinion-typed facts with a non-null sentiment. The
    # tension semantics only make sense over opinion / recommendation
    # facts; observation / event facts are factual reports, not
    # contestable positions.
    opinion_facts: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        ft = str(f.get("fact_type") or "").strip().lower()
        if ft not in _OPINION_TYPES:
            continue
        sentiment = f.get("sentiment")
        if not isinstance(sentiment, str) or not sentiment.strip():
            continue
        opinion_facts.append(f)

    if len(opinion_facts) < 2:
        return {"tensions": out_tensions, "fact_annotations": out_annotations}

    # Pair-wise comparison — O(n²) over the opinion subset. Typical
    # clusters have ≤10 opinion facts so this is ~50 comparisons,
    # cheap enough that the brute force is the simplest correct
    # approach.
    seen_pairs: set[frozenset[str]] = set()
    for i in range(len(opinion_facts)):
        for j in range(i + 1, len(opinion_facts)):
            fa = opinion_facts[i]
            fb = opinion_facts[j]
            sa = str(fa.get("sentiment") or "").strip().lower()
            sb = str(fb.get("sentiment") or "").strip().lower()
            if not _opposing_sentiments(sa, sb):
                continue
            shared = _shared_entities(
                fa.get("entity_tags") or [],
                fb.get("entity_tags") or [],
            )
            if len(shared) < _MIN_SHARED_ENTITY_OVERLAP:
                continue

            fa_id = str(fa.get("id") or fa.get("fact_id") or "")
            fb_id = str(fb.get("id") or fb.get("fact_id") or "")
            if not fa_id or not fb_id:
                continue
            pair_key = frozenset({fa_id, fb_id})
            if pair_key in seen_pairs:
                continue
            seen_pairs.add(pair_key)

            tension_id = _stable_tension_id(fa_id, fb_id)

            # Title — best-effort first sentence of the fact whose
            # text has more substance (longer wins). Tie-broken by
            # lexicographic order so the choice is deterministic.
            text_a = str(fa.get("memory_text") or "")
            text_b = str(fb.get("memory_text") or "")
            primary_text = text_a if len(text_a) >= len(text_b) else text_b
            title = _first_sentence_capped(primary_text)

            ts_a = str(fa.get("message_ts") or fa.get("timestamp") or fa.get("date") or "")
            ts_b = str(fb.get("message_ts") or fb.get("timestamp") or fb.get("date") or "")
            since = _earlier_iso_date(ts_a, ts_b)

            def _stance(fact: dict[str, Any]) -> str:
                """Best-effort stance summary — first sentence of the
                fact text, capped, safety-stripped."""
                return _first_sentence_capped(str(fact.get("memory_text") or ""))

            def _author(fact: dict[str, Any]) -> str:
                return _strip_safety_markers(
                    str(
                        fact.get("author_name") or fact.get("user_name") or fact.get("author") or ""
                    )
                )

            positions = [
                {
                    "author": _author(fa),
                    "stance": _stance(fa),
                    "fact_id": fa_id,
                },
                {
                    "author": _author(fb),
                    "stance": _stance(fb),
                    "fact_id": fb_id,
                },
            ]

            out_tensions.append(
                {
                    "tension_id": tension_id,
                    "title": title,
                    "since": since,
                    "status": "open",
                    "positions": positions,
                }
            )

            # Record annotations only for facts not already annotated
            # by an earlier tension. The first tension encountered
            # wins so AtomicFact-level fields stay deterministic.
            if fa_id not in out_annotations:
                out_annotations[fa_id] = {
                    "tension_id": tension_id,
                    "contradicts_fact_id": fb_id,
                }
            if fb_id not in out_annotations:
                out_annotations[fb_id] = {
                    "tension_id": tension_id,
                    "contradicts_fact_id": fa_id,
                }

    return {"tensions": out_tensions, "fact_annotations": out_annotations}


__all__ = ["detect_tensions"]
