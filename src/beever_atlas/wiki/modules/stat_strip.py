"""``stat_strip`` module — frontend renderer.

Surfaces numeric values that are buried in fact text as headline
cards at the top of the page. The numbers ARE the headline for
metric-heavy topics (campaign reports, growth dashboards, perf
runs) — pulling them out gives the reader an at-a-glance summary
before they read the prose.

Detection is conservative on purpose — false positives spam the
header with meaningless cards (e.g., turning "5 people attended"
into a stat card looks worse than missing it entirely). Patterns:
  - Comma-grouped integers: ``2,396``
  - k/M-suffixed numbers: ``534k``, ``1.2M``
  - Currency: ``HK$130k``, ``$50K``, ``£1,200``, ``€42k``

Plain integers must be ≥ 100 to qualify (smaller numbers create
too much noise — "I sent 5 emails" should not surface as a stat).

Renderer lives in
``web/src/components/wiki/modules/StatStripModule.tsx`` —
this file is purely a builder.
"""

from __future__ import annotations

import re
from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

# Maximum stats per page. The strip is meant to be a glanceable
# headline, not a kitchen sink — beyond 5 the cards shrink to
# illegibility on tablet/mobile.
_MAX_STATS = 5

# Currency prefixes we recognise. Order matters: longer prefixes
# first so ``HK$`` matches before ``$``.
_CURRENCY_PREFIXES = (r"HK\$", r"US\$", r"\$", r"£", r"€")
_CURRENCY_GROUP = "(?:" + "|".join(_CURRENCY_PREFIXES) + ")"

# Combined pattern. Three alternatives:
#  1. Currency-prefixed number (HK$130k, $1,200.50, etc.)
#  2. Comma-grouped integer (2,396 — at least one comma group)
#  3. k/M-suffixed number (534k, 1.2M)
# The plain-integer (≥100) case is handled separately so we can
# enforce the magnitude floor without over-matching e.g. dates.
_NUMERIC_RE = re.compile(
    r"(?P<currency>" + _CURRENCY_GROUP + r"\d+(?:,\d{3})*(?:\.\d+)?[kKmM]?)"
    r"|(?P<grouped>\b\d{1,3}(?:,\d{3})+\b)"
    r"|(?P<suffix>\b\d+(?:\.\d+)?[kKmM]\b)"
)
# Plain integer ≥ 100. Standalone pattern so the magnitude check is
# explicit (and so we don't accidentally match e.g. "5 people").
_PLAIN_INT_RE = re.compile(r"\b\d{3,}\b")

# Words we strip from the start of a label — these are filler
# verbs/prepositions that creep in when we grab the words right
# after a number ("...generated 2,396 actions across the campaign"
# would otherwise produce label "across the campaign" if we matched
# greedily; we want "actions").
_LABEL_STOPWORDS = {
    "of",
    "in",
    "on",
    "for",
    "to",
    "from",
    "by",
    "with",
    "across",
    "the",
    "a",
    "an",
    "this",
    "that",
    "these",
    "those",
    "his",
    "her",
    "their",
    "its",
    "our",
    "my",
    "your",
}

# Words that disqualify a noun match — these are dates / times /
# generic words that surface from regex hits but aren't useful as
# stat labels.
_LABEL_BLOCKLIST = {
    "am",
    "pm",
    "gmt",
    "utc",
    "mon",
    "tue",
    "wed",
    "thu",
    "fri",
    "sat",
    "sun",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
    "jan",
    "feb",
    "mar",
    "apr",
    "may",
    "jun",
    "jul",
    "aug",
    "sep",
    "oct",
    "nov",
    "dec",
    "january",
    "february",
    "march",
    "april",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
    "year",
    "years",
    "month",
    "months",
    "week",
    "weeks",
    "day",
    "days",
    "hour",
    "hours",
    "minute",
    "minutes",
    "second",
    "seconds",
}


def _coerce_raw_value(value: str) -> float | None:
    """Convert a captured numeric string (with optional currency
    prefix, commas, and k/M suffix) into a float for sorting /
    future trend analysis. Returns ``None`` when conversion fails.
    """
    if not value:
        return None
    s = str(value).strip()
    # Strip currency prefix.
    for prefix in ("HK$", "US$", "$", "£", "€"):
        if s.startswith(prefix):
            s = s[len(prefix) :]
            break
    s = s.replace(",", "")
    multiplier = 1.0
    if s and s[-1] in {"k", "K"}:
        multiplier = 1_000.0
        s = s[:-1]
    elif s and s[-1] in {"m", "M"}:
        multiplier = 1_000_000.0
        s = s[:-1]
    try:
        return float(s) * multiplier
    except (TypeError, ValueError):
        return None


def _extract_label(text: str, start: int, end: int) -> str:
    """Extract the noun (or noun phrase) immediately following the
    matched number. Naïve heuristic: 1–3 words after the match,
    skipping leading stopwords. Returns ``""`` when no usable label
    is found within the next ~40 chars.
    """
    if end >= len(text):
        return ""
    # Take a window after the match for word extraction.
    window = text[end : end + 60]
    # Match up to 3 word-tokens — alpha characters + optional
    # internal hyphen/apostrophe (e.g., "open-rate", "user's").
    words = re.findall(r"[A-Za-z][A-Za-z'\-]*", window)
    if not words:
        return ""
    # Drop leading filler words.
    while words and words[0].lower() in _LABEL_STOPWORDS:
        words.pop(0)
    if not words:
        return ""
    # Take up to 3 words — but stop at a stopword or blocklist hit
    # so labels stay tight ("actions" not "actions across").
    out: list[str] = []
    for w in words[:3]:
        wl = w.lower()
        if wl in _LABEL_STOPWORDS:
            break
        if wl in _LABEL_BLOCKLIST:
            # First-token blocklist hit kills the whole label
            # (e.g., number followed by "minutes" — likely a
            # duration, not a metric we want to feature).
            if not out:
                return ""
            break
        out.append(w)
    return " ".join(out).strip(" ,.;:")


def _qualifies_plain_int(value_str: str) -> bool:
    """Plain integer must parse and be ≥ 100. Below that the noise
    rate explodes (counts of items, ages, percentages without the
    ``%`` sign all fall in 1–99)."""
    try:
        return int(value_str) >= 100
    except (TypeError, ValueError):
        return False


def _has_numeric_match(text: str) -> bool:
    """Return True if ``text`` contains at least one stat-shaped
    numeric. Used by ``count_numeric_facts`` for signal computation.
    """
    if not text:
        return False
    if _NUMERIC_RE.search(text):
        return True
    # Plain integer ≥ 100 path.
    for m in _PLAIN_INT_RE.finditer(text):
        if _qualifies_plain_int(m.group(0)):
            return True
    return False


def count_numeric_facts(facts: list[dict] | None) -> int:
    """Count facts whose ``memory_text`` contains at least one
    stat-shaped numeric value. Populates the ``numeric_fact_count``
    signal used by ``stat_strip``'s eligibility predicate."""
    if not isinstance(facts, list):
        return 0
    n = 0
    for f in facts:
        if not isinstance(f, dict):
            continue
        body = _strip_safety_markers(f.get("memory_text") or f.get("fact") or f.get("text") or "")
        if _has_numeric_match(body):
            n += 1
    return n


def _iter_matches(text: str) -> list[tuple[str, int, int]]:
    """Yield (matched_value, start, end) tuples for every stat-shaped
    numeric in ``text``. Returns matches in source order. Plain
    integers are checked last so currency / suffixed forms win when
    they overlap (e.g., ``$2,396`` should match the currency form,
    not the plain integer ``2``)."""
    out: list[tuple[str, int, int]] = []
    occupied: list[tuple[int, int]] = []
    for m in _NUMERIC_RE.finditer(text):
        out.append((m.group(0), m.start(), m.end()))
        occupied.append((m.start(), m.end()))
    for m in _PLAIN_INT_RE.finditer(text):
        if not _qualifies_plain_int(m.group(0)):
            continue
        # Skip if already covered by a richer match (currency, etc.).
        if any(s <= m.start() < e for s, e in occupied):
            continue
        out.append((m.group(0), m.start(), m.end()))
    out.sort(key=lambda t: t[1])
    return out


def _date_range_from_facts(facts: list[dict]) -> dict[str, str]:
    """Compute the ``period`` field from the facts' message_ts /
    timestamp / date fields. Returns first-and-last (ISO date prefix)
    or empty strings when no parsable timestamps are present."""
    dates: list[str] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        ts = f.get("message_ts") or f.get("timestamp") or f.get("date") or ""
        if not ts:
            continue
        # ISO prefix only (10 chars). Lexical order matches calendar
        # order, so we don't need full datetime parsing.
        s = str(ts)[:10]
        if s:
            dates.append(s)
    if not dates:
        return {"from": "", "to": ""}
    dates.sort()
    return {"from": dates[0], "to": dates[-1]}


def _collect_structured_numerics(facts: list[dict]) -> list[dict[str, Any]]:
    """Phase 3 — pull structured ``numeric_values`` off each fact.

    The fact extractor populates ``numeric_values`` as a list of dicts
    with keys ``label`` / ``value`` / ``raw_value`` / ``unit``.
    Returns a flat list of stat-strip-shaped dicts, preserving the
    fact-order the LLM emitted (which approximates author intent
    better than regex source order). Items missing ``value`` or
    ``label`` are skipped so a malformed extraction can't break the
    builder.
    """
    out: list[dict[str, Any]] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        nvs = f.get("numeric_values")
        if not isinstance(nvs, list):
            continue
        fact_id = str(f.get("fact_id") or f.get("id") or "")
        for nv in nvs:
            if not isinstance(nv, dict):
                continue
            raw_value = nv.get("value")
            raw_label = nv.get("label")
            if not isinstance(raw_value, str) or not isinstance(raw_label, str):
                continue
            value = _strip_safety_markers(raw_value).strip()
            label = _strip_safety_markers(raw_label).strip()
            if not value or not label:
                continue
            out.append(
                {
                    "value": value,
                    "label": label,
                    "fact_id": fact_id,
                    "raw_value": nv.get("raw_value"),
                    "unit": nv.get("unit"),
                    "_source_fact": f,
                }
            )
    return out


def build_stat_strip_data(facts: list[dict] | None) -> dict[str, Any]:
    """Build the payload the React StatStripModule consumes.

    Phase 3 — prefers structured ``numeric_values`` from the extractor
    when any are present; falls back to regex detection on
    ``memory_text`` when the structured path produces nothing (covers
    pre-Phase-3 documents and facts where the extractor didn't classify
    the number).

    The structured-first path dedups by ``(value, label)``, caps at 5
    entries, and attaches a ``period`` derived from contributing facts.

    Returns:
        {
          "label": "Stats",
          "renderer_kind": "frontend",
          "stats": [
            {
              "value": "2,396",
              "label": "actions",
              "fact_id": "f_xyz",
              "raw_value": 2396.0,
              "unit": "USD" | null
            },
            ...
          ],
          "period": {"from": "2026-04-26", "to": "2026-05-02"}
        }
    """
    if not isinstance(facts, list):
        return {
            "label": "Stats",
            "renderer_kind": "frontend",
            "stats": [],
            "period": {"from": "", "to": ""},
        }

    # ---- Phase 3 structured-first path -------------------------------
    structured = _collect_structured_numerics([f for f in facts if isinstance(f, dict)])
    if structured:
        seen: set[tuple[str, str]] = set()
        deduped: list[dict[str, Any]] = []
        contributing: list[dict] = []
        for s in structured:
            key = (s["value"], s["label"].lower())
            if key in seen:
                continue
            seen.add(key)
            src = s.pop("_source_fact", None)
            deduped.append(s)
            if isinstance(src, dict) and src not in contributing:
                contributing.append(src)
            if len(deduped) >= _MAX_STATS:
                break
        period = (
            _date_range_from_facts(contributing)
            if contributing
            else {
                "from": "",
                "to": "",
            }
        )
        return {
            "label": "Stats",
            "renderer_kind": "frontend",
            "stats": deduped,
            "period": period,
        }

    # Sort facts by importance DESC so the highest-importance facts
    # win the limited stat slots. We re-use the importance bucket
    # rank used elsewhere in the codebase (critical/high/medium/low
    # OR numeric ≥9/≥7/≥4/else).
    def _rank(f: dict) -> float:
        v = f.get("importance")
        if isinstance(v, (int, float)):
            try:
                return float(v)
            except (TypeError, ValueError):
                return 0.0
        if isinstance(v, str):
            s = v.strip().lower()
            return {"critical": 9.0, "high": 7.0, "medium": 4.0, "low": 1.0}.get(s, 0.0)
        return 0.0

    sorted_facts = sorted(
        (f for f in facts if isinstance(f, dict)),
        key=_rank,
        reverse=True,
    )

    stats: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    contributing_facts: list[dict] = []

    for f in sorted_facts:
        body = _strip_safety_markers(f.get("memory_text") or f.get("fact") or f.get("text") or "")
        if not body:
            continue
        fact_id = str(f.get("fact_id") or f.get("id") or "")
        any_used = False
        for value, start, end in _iter_matches(body):
            label = _extract_label(body, start, end)
            if not label:
                # No useful label — without a noun the card is
                # meaningless ("2,396" with no context). Skip rather
                # than render a labelless card.
                continue
            key = (value, label.lower())
            if key in seen:
                continue
            seen.add(key)
            stats.append(
                {
                    "value": value,
                    "label": label,
                    "fact_id": fact_id,
                    "raw_value": _coerce_raw_value(value),
                }
            )
            any_used = True
            if len(stats) >= _MAX_STATS:
                break
        if any_used:
            contributing_facts.append(f)
        if len(stats) >= _MAX_STATS:
            break

    period = (
        _date_range_from_facts(contributing_facts)
        if contributing_facts
        else {
            "from": "",
            "to": "",
        }
    )

    return {
        "label": "Stats",
        "renderer_kind": "frontend",
        "stats": stats,
        "period": period,
    }
