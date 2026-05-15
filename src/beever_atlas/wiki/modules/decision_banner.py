"""``decision_banner`` module — frontend renderer.

Spotlights the SINGLE decision on a Decision-archetype topic page.
Replaces the burying of a decision in a key_facts row with an
attention-grabbing banner: ✅ DECIDED ribbon, the headline decision,
optional body paragraph, and (Phase 3) rationale / alternatives /
open consequences.

Today (Phase 4 prep) the builder uses ONLY existing fields on
``AtomicFact``. The richer enrichment fields (``rationale``,
``alternatives_considered``, ``consequences_open``) are placeholders
that emit ``null`` / ``[]`` so the schema is forward-compatible —
Phase 3 will populate them without changing the contract.

Renderer lives in
``web/src/components/wiki/modules/DecisionBannerModule.tsx`` —
this file is purely a builder.
"""

from __future__ import annotations

import re
from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

# First-sentence detection — same pattern as ``key_facts._first_sentence``
# but kept inline so the two modules don't develop accidental coupling.
_FIRST_SENTENCE_RE = re.compile(r"^(.*?[.!?])(?:\s|$)")


def _split_sentence(text: str) -> tuple[str, str]:
    """Split ``text`` into (first sentence, rest).

    The first sentence is capitalized and trimmed. The rest is
    returned as-is (no capitalization) — it forms the optional body
    paragraph the frontend renders below the headline. When ``text``
    is a single sentence (or whitespace-only), ``rest`` is ``""``.
    """
    if not text:
        return "", ""
    s = " ".join(str(text).split())
    if not s:
        return "", ""
    m = _FIRST_SENTENCE_RE.match(s)
    if m:
        first = m.group(1).strip()
        rest = s[m.end() :].strip()
    else:
        # No sentence terminator found — treat the whole text as one
        # sentence with no body.
        first = s
        rest = ""
    if first:
        first = first[0].upper() + first[1:]
    return first, rest


def _iso_date(ts: str) -> str:
    """Extract the ISO date prefix (YYYY-MM-DD) from a timestamp.

    Accepts ISO timestamps (``2026-04-29T10:32:00Z``) and bare dates
    (``2026-04-29``). Returns ``""`` when ``ts`` is empty or doesn't
    parse to a recognisable date prefix — the frontend hides the date
    chip when empty.
    """
    if not ts:
        return ""
    s = str(ts).strip()
    if not s:
        return ""
    # Take the first 10 chars and verify they look like YYYY-MM-DD.
    head = s[:10]
    if len(head) == 10 and head[4] == "-" and head[7] == "-":
        try:
            int(head[:4])
            int(head[5:7])
            int(head[8:10])
            return head
        except ValueError:
            return ""
    return ""


def _importance_score(value: Any) -> float:
    """Coerce the fact's importance to a numeric score for ranking.

    Numeric inputs flow through directly. String inputs map to the
    same severity buckets ``key_facts`` uses (critical=10, high=8,
    medium=5, low=2). Unknown / missing → 0.
    """
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0
    if isinstance(value, str):
        s = value.strip().lower()
        if s in {"critical"}:
            return 10.0
        if s in {"high"}:
            return 8.0
        if s in {"medium"}:
            return 5.0
        if s in {"low"}:
            return 2.0
        try:
            return float(s)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _pick_top_decision(facts: list[Any]) -> dict[str, Any] | None:
    """Return the highest-importance ``fact_type=="decision"`` fact.

    Ties broken by: longer ``memory_text`` (more substance) then by
    earliest ``message_ts`` (first decision wins). Returns ``None`` if
    no decision-typed fact is present in the list.
    """
    candidates: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        ft = str(f.get("fact_type") or "").strip().lower()
        if ft != "decision":
            continue
        candidates.append(f)
    if not candidates:
        return None
    candidates.sort(
        key=lambda f: (
            _importance_score(f.get("importance")),
            len(str(f.get("memory_text") or "")),
            # Negate ts so earlier dates rank higher when importance ties.
            # Strings sort lexicographically; we use a high sentinel for
            # missing dates so they sort last.
            -ord(str(f.get("message_ts") or "￿")[0]) if str(f.get("message_ts") or "") else -1,
        ),
        reverse=True,
    )
    return candidates[0]


def build_decision_banner_data(
    facts: list[Any] | None,
    member_facts: list[Any] | None = None,
) -> dict[str, Any]:
    """Build the payload the React DecisionBannerModule consumes.

    Pure function — no IO, no LLM. Selects the single highest-
    importance ``decision``-typed fact from ``facts`` (or
    ``member_facts`` as a fallback) and shapes it into the banner
    contract. Phase 3 fields (``rationale``, ``alternatives_rejected``,
    ``consequences_open``) emit ``null`` / ``[]`` so the schema is
    forward-compatible.

    Returns:
        {
          "label": "Decision",
          "renderer_kind": "frontend",
          "decision": "<first sentence, capitalized>",
          "body": "<rest of memory_text, or empty>",
          "decided_by": {"name": "...", "fact_id": "..."},
          "decided_at": "YYYY-MM-DD",
          "rationale": null,                  # Phase 3 placeholder
          "alternatives_rejected": [],        # Phase 3 placeholder
          "consequences_open": [],            # Phase 3 placeholder
          "fact_id": "f_..."
        }

    When no decision fact exists, returns the empty banner shape (the
    frontend renders nothing — defensive against the planner picking
    the module despite the predicate failing).
    """
    pool: list[Any] = []
    if isinstance(facts, list):
        pool.extend(facts)
    if isinstance(member_facts, list):
        pool.extend(member_facts)

    decision_fact = _pick_top_decision(pool)
    if decision_fact is None:
        return {
            "label": "Decision",
            "renderer_kind": "frontend",
            "decision": "",
            "body": "",
            "decided_by": {"name": "", "fact_id": ""},
            "decided_at": "",
            "rationale": None,
            "alternatives_rejected": [],
            "consequences_open": [],
            "fact_id": "",
        }

    body_text = _strip_safety_markers(
        decision_fact.get("memory_text")
        or decision_fact.get("fact")
        or decision_fact.get("text")
        or ""
    )
    headline, rest = _split_sentence(body_text)

    fact_id = str(decision_fact.get("fact_id") or decision_fact.get("id") or "")
    author_name = str(
        decision_fact.get("author_name")
        or decision_fact.get("user_name")
        or decision_fact.get("author")
        or ""
    )
    decided_at = _iso_date(
        str(
            decision_fact.get("message_ts")
            or decision_fact.get("timestamp")
            or decision_fact.get("date")
            or ""
        )
    )
    permalink = str(decision_fact.get("permalink") or decision_fact.get("source_url") or "")

    # Phase 3 fields — read from structured extraction. The fields
    # default to None / [] for pre-Phase-3 documents so the renderer
    # hides empty rows automatically. We strip safety markers and
    # whitespace defensively because the LLM output traverses the
    # same prompt-safety chain as memory_text.
    raw_rationale = decision_fact.get("rationale")
    rationale: str | None = None
    if isinstance(raw_rationale, str):
        cleaned_rationale = _strip_safety_markers(raw_rationale).strip()
        rationale = cleaned_rationale or None

    alternatives_rejected: list[str] = []
    raw_alts = decision_fact.get("alternatives_considered")
    if isinstance(raw_alts, list):
        for alt in raw_alts:
            if isinstance(alt, str):
                cleaned = _strip_safety_markers(alt).strip()
                if cleaned:
                    alternatives_rejected.append(cleaned)

    consequences_open: list[str] = []
    raw_cons = decision_fact.get("consequences_open")
    if isinstance(raw_cons, list):
        for cons in raw_cons:
            if isinstance(cons, str):
                cleaned = _strip_safety_markers(cons).strip()
                if cleaned:
                    consequences_open.append(cleaned)

    return {
        "label": "Decision",
        "renderer_kind": "frontend",
        "decision": headline,
        "body": rest,
        "decided_by": {"name": author_name, "fact_id": fact_id},
        "decided_at": decided_at,
        "rationale": rationale,
        "alternatives_rejected": alternatives_rejected,
        "consequences_open": consequences_open,
        "fact_id": fact_id,
        "source_url": permalink,
    }
