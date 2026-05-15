"""Narrative-article validator — citation discipline + word caps.

Spec: ``openspec/changes/wiki-narrative-articles/specs/wiki-narrative-articles/spec.md``
covers the requirements implemented here:

  - Inline citation discipline (every paragraph cites ≥1 fact_id).
  - Forbidden activity-log narration phrases ("shared a link",
    "noted that", etc.) drop the offending paragraph.
  - Word caps per section (150-400) and per article (1500-3000
    typical, 5000+ landmark).
  - Inference paragraphs (``is_inference: true``) MUST still cite
    ≥1 fact_id; uncited inference paragraphs are dropped.
  - Citation-coverage gate at 80% — articles below that threshold
    return ``rejected: true`` so the orchestrator falls back to the
    module-only rendering.

The validator is FAIL-SAFE: any unexpected exception falls back to
``([], {rejected: True, reason: "validator_exception"})`` rather than
raising. The orchestrator persists ``narrative_sections=[]`` in that
case and the page renders module-only.

Public surface: ``validate_narrative_sections(sections, facts)`` →
``(cleaned_sections, telemetry)``. Pure function — no IO, no LLM.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

logger = logging.getLogger(__name__)


# Activity-log narration phrases that must never appear in narrative
# paragraphs. We use a word-boundary regex (M-1) instead of plain
# substring match so legitimate words containing these substrings
# (e.g., "denoted that the X", "reposted about Y") are NOT flagged.
# The phrase set is closed — to add a new forbidden phrase, extend
# the alternation below. Compiled once at module load.
_FORBIDDEN_PHRASE_RE = re.compile(
    r"\b(?:"
    r"shared (?:a|an) (?:link|article)|"
    r"noted that|"
    r"mentioned that|"
    r"posted about|"
    r"presented that"
    r")\b",
    re.IGNORECASE,
)

# Section / article word-count thresholds. Sections over the soft cap
# get truncated at sentence boundary; the article-over-cap is logged
# but only rejected on egregious bloat (> 6000 words for non-landmark).
_SECTION_MIN_WORDS = 150
_SECTION_SOFT_MAX_WORDS = 400
_ARTICLE_MIN_WORDS = 1500
_ARTICLE_TYPICAL_MAX_WORDS = 3000
_ARTICLE_LANDMARK_MAX_WORDS = 5000
_ARTICLE_HARD_MAX_WORDS = 6000

# Citation-coverage gate thresholds, tiered by input-paragraph count.
# Small topics (< 5 paragraphs) get a relaxed threshold because the
# LLM sometimes omits the citations array on 1-2 paragraphs, and
# rejecting the whole article is too aggressive for thin inputs.
_CITATION_COVERAGE_GATE = 0.80  # > 10 paragraphs (large topics)
_CITATION_COVERAGE_GATE_MID = 0.70  # 5-10 paragraphs
_CITATION_COVERAGE_GATE_SMALL = 0.60  # < 5 paragraphs

# Sentence boundary detection — period / question mark / exclamation
# followed by whitespace. Conservative; captures the most common
# patterns without trying to handle abbreviations or quoted speech.
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")

# Anchor format (M-8): kebab-case alphanumeric, must start with a
# letter or digit, max 24 chars. Matches the v3 prompt's anchor
# guidance and keeps DOM ``id`` lookups (``getElementById``) reliable
# regardless of what the LLM emits. Compiled once at module load.
_VALID_ANCHOR_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")


def _word_count(text: str) -> int:
    """Cheap word counter — splits on whitespace. Empty / non-string
    returns 0 so the cap-checking math is total."""
    if not isinstance(text, str):
        return 0
    return len(text.split())


def _truncate_to_word_cap(text: str, cap: int) -> str:
    """Truncate ``text`` to at most ``cap`` words, cutting at the last
    sentence boundary that fits.

    Returns the original text when already under cap. Falls back to a
    word-boundary cut + ellipsis when no sentence terminator falls in
    the budget. The result is always non-empty (the input was checked
    by the caller).
    """
    if _word_count(text) <= cap:
        return text
    sentences = _SENTENCE_END_RE.split(text)
    accumulated_words = 0
    kept: list[str] = []
    for sent in sentences:
        sent_words = _word_count(sent)
        if accumulated_words + sent_words > cap:
            break
        kept.append(sent)
        accumulated_words += sent_words
    if not kept:
        # No sentence boundary fit the budget — hard truncate at word
        # boundary + ellipsis so we never overflow the cap.
        words = text.split()[:cap]
        return " ".join(words).rstrip(".,;:!? ") + "…"
    return " ".join(kept).strip()


def _sanitize_anchor(raw: str, fallback_idx: int) -> str:
    """Return a valid kebab-case anchor (M-8 — defensive sanitisation).

    Tries the raw value first; if invalid, derives from the heading
    (slugify); if heading also fails, falls back to ``section-N``.
    Never raises — anchor is always non-empty and DOM-safe so the
    frontend's ``getElementById`` scroll-snap stays reliable even
    when the LLM emits malformed or HTML-injection-like anchors.

    The frontend mirrors this regex in
    ``NarrativeArticleModule.tsx::sanitizeAnchor`` for
    defense-in-depth on already-persisted articles.
    """
    candidate = (raw or "").strip().lower()
    if _VALID_ANCHOR_RE.match(candidate):
        return candidate
    # Try slugifying — keep alphanumerics + dashes, collapse runs of
    # non-alphanumerics into a single dash, trim, cap at 24 chars.
    slug = re.sub(r"[^a-z0-9]+", "-", candidate).strip("-")[:24]
    if _VALID_ANCHOR_RE.match(slug):
        return slug
    return f"section-{fallback_idx}"


def _paragraph_is_uncited(paragraph: dict[str, Any]) -> bool:
    """True when the paragraph has zero non-empty fact_id citations.

    Tolerant: a missing or non-list ``citations`` field counts as
    uncited (the v3 prompt is explicit about emitting the list, but
    we must not rely on the LLM's compliance).
    """
    citations = paragraph.get("citations")
    if not isinstance(citations, list):
        return True
    return not any(c for c in citations if isinstance(c, str) and c.strip())


def _paragraph_has_forbidden_phrase(paragraph: dict[str, Any]) -> str | None:
    """Return the offending phrase when the paragraph contains a
    forbidden activity-narration phrase, else None.

    Uses ``_FORBIDDEN_PHRASE_RE`` (word-boundary, case-insensitive) so
    legitimate words containing these substrings — e.g. "denoted that
    the X", "reposted about Y" — are NOT flagged. We return the
    matched phrase so the caller can include it in the structured
    drop-log line — useful for soak telemetry.
    """
    text = paragraph.get("text") or ""
    if not isinstance(text, str):
        return None
    match = _FORBIDDEN_PHRASE_RE.search(text)
    return match.group(0) if match else None


def _validate_section(
    section: dict[str, Any],
    *,
    paragraphs_dropped: list[int],
    section_idx: int = 1,
) -> dict[str, Any] | None:
    """Validate one section. Returns the cleaned section payload, or
    ``None`` when the section has no surviving paragraphs (caller
    should drop the whole section in that case).

    Mutates ``paragraphs_dropped`` (a single-element list used as a
    mutable accumulator) so the caller can surface a total count in
    telemetry without redundant traversal.

    ``section_idx`` is the 1-based position of this section in the
    article — used as the fallback for anchor sanitisation (M-8) so a
    section with a malformed anchor still gets a stable ``section-N``
    identifier rather than dropping the section entirely.
    """
    if not isinstance(section, dict):
        return None
    raw_anchor = str(section.get("anchor") or "").strip()
    # ``_strip_safety_markers`` is the canonical scrub point for
    # prompt-safety wrappers (``<untrusted>``, ``<sanitized>``,
    # ``<external>``). Applying it here, BEFORE persistence, means
    # downstream consumers (frontend builder, MCP ``read_wiki_section``
    # tool, drift comparator) receive clean text without each
    # re-implementing the strip. See H-6 in the
    # ``wiki-narrative-articles`` code review.
    heading = _strip_safety_markers(section.get("heading") or "")
    if not heading:
        logger.info(
            "narrative_section_dropped reason=missing_anchor_or_heading anchor=%s",
            raw_anchor or "<empty>",
        )
        return None
    # M-8: sanitise the anchor against ``_VALID_ANCHOR_RE``. Never
    # drops the section on a bad anchor — falls back to a derived slug
    # or ``section-N``. Heading was already validated above, so an
    # empty raw_anchor only triggers the slug-from-heading path when
    # the heading itself yields valid characters; otherwise we end up
    # at ``section-N``.
    anchor = _sanitize_anchor(raw_anchor or heading, section_idx)

    paragraphs_in = section.get("paragraphs") or []
    if not isinstance(paragraphs_in, list):
        paragraphs_in = []

    cleaned_paragraphs: list[dict[str, Any]] = []
    for paragraph in paragraphs_in:
        if not isinstance(paragraph, dict):
            paragraphs_dropped[0] += 1
            continue
        text_raw = paragraph.get("text") or ""
        # Scrub safety markers before any other check — the
        # forbidden-phrase + uncited filters need to see the cleaned
        # text, and persisting the cleaned form means the frontend
        # builder + MCP tool inherit the strip without duplicating it.
        text = _strip_safety_markers(text_raw) if isinstance(text_raw, str) else ""
        if not text:
            paragraphs_dropped[0] += 1
            continue
        # Mutate the input dict so subsequent forbidden-phrase and
        # citation checks operate on the cleaned text. The dict is the
        # validator's local copy from the caller's list — safe to
        # adjust for the duration of the loop iteration.
        paragraph = dict(paragraph)
        paragraph["text"] = text
        # 1. Forbidden-phrase filter — drop activity-narration paragraphs
        #    BEFORE the citation check so the structured log carries the
        #    most informative reason.
        forbidden = _paragraph_has_forbidden_phrase(paragraph)
        if forbidden is not None:
            logger.info(
                "narrative_paragraph_dropped reason=activity_narration phrase=%r section=%s",
                forbidden,
                anchor,
            )
            paragraphs_dropped[0] += 1
            continue
        # 2. Citation discipline — drop uncited paragraphs. Inference
        #    paragraphs MUST still cite (Decision 3 in the design doc).
        if _paragraph_is_uncited(paragraph):
            is_inf = bool(paragraph.get("is_inference"))
            reason = "uncited_inference" if is_inf else "no_citations"
            logger.info(
                "narrative_paragraph_dropped reason=%s section=%s",
                reason,
                anchor,
            )
            paragraphs_dropped[0] += 1
            continue
        # Coerce the cleaned paragraph to the canonical shape. The
        # builder also normalises later, but doing it here keeps the
        # validator output independently consumable (e.g., by tests).
        citations_raw = paragraph.get("citations") or []
        citations = [str(c).strip() for c in citations_raw if isinstance(c, str) and c.strip()]
        cleaned_paragraphs.append(
            {
                "text": text.strip(),
                "citations": citations,
                "is_inference": bool(paragraph.get("is_inference")),
            }
        )

    if not cleaned_paragraphs:
        logger.info(
            "narrative_section_dropped reason=no_surviving_paragraphs section=%s",
            anchor,
        )
        return None

    # 3. Word-cap enforcement per section. Reconstruct the section
    #    text, count words, and if over the soft cap, truncate the
    #    LAST paragraph at a sentence boundary so the section fits.
    #
    # H-7 contract note: paragraph-level ``citations`` are metadata,
    # NOT inline-reference tokens scraped from ``text``. When the last
    # paragraph is truncated below, the ``citations`` list is left
    # untouched — the validator does NOT re-derive citations by
    # parsing ``[f_xxx]`` patterns out of the surviving text because
    # the v3 prompt's citation discipline persists ``citations`` as a
    # structured list (paragraphs cite by id, not by inline pattern).
    # Display chips therefore reflect the paragraph's full claim set,
    # not a substring lookup. Future tooling that wants chip-text
    # alignment must do its own substring matching at render time.
    section_text = " ".join(p["text"] for p in cleaned_paragraphs)
    section_words = _word_count(section_text)
    if section_words > _SECTION_SOFT_MAX_WORDS:
        logger.info(
            "narrative_section_over_cap section=%s words=%d cap=%d",
            anchor,
            section_words,
            _SECTION_SOFT_MAX_WORDS,
        )
        # Truncate the last paragraph so the section fits. We keep all
        # earlier paragraphs intact; this preserves citation coverage
        # and avoids a global re-balance. The truncated paragraph's
        # ``citations`` list is preserved (see H-7 contract note above).
        excess = section_words - _SECTION_SOFT_MAX_WORDS
        last = cleaned_paragraphs[-1]
        last_words = _word_count(last["text"])
        if last_words > excess:
            new_word_budget = max(20, last_words - excess)
            cleaned_paragraphs[-1] = {
                **last,
                "text": _truncate_to_word_cap(last["text"], new_word_budget),
            }

    # 4. Build union-citations + per-section coverage. ``coverage`` is
    #    the fraction of paragraphs that have ≥1 citation — after the
    #    forbidden / uncited drops above, this is always 1.0 today,
    #    but persisting the value lets the frontend surface it in the
    #    soak dashboard without recomputing.
    seen: set[str] = set()
    union_citations: list[str] = []
    for p in cleaned_paragraphs:
        for c in p["citations"]:
            if c and c not in seen:
                seen.add(c)
                union_citations.append(c)
    paragraphs_with_citations = sum(1 for p in cleaned_paragraphs if p["citations"])
    coverage = paragraphs_with_citations / len(cleaned_paragraphs) if cleaned_paragraphs else 0.0

    # 5. Pass through the visual untouched (the builder normalises it
    #    further; we keep the validator focused on text discipline).
    visual = section.get("visual")
    if not isinstance(visual, dict):
        visual = None

    return {
        "anchor": anchor,
        "heading": heading,
        "paragraphs": cleaned_paragraphs,
        "citations": union_citations,
        "visual": visual,
        "citation_coverage": coverage,
    }


def validate_narrative_sections(
    sections: list[dict] | None,
    facts: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """Validate + clean the LLM's ``narrative_sections`` output.

    Returns ``(cleaned_sections, telemetry)``. When the citation-
    coverage gate fails, the cleaned list is empty and telemetry
    carries ``rejected=True`` so the orchestrator can persist
    ``narrative_sections=[]`` and the page falls back to module-only
    rendering.

    Telemetry shape::

        {
          "rejected": bool,
          "reason": str,            # "ok" | "low_citation_coverage" | "validator_exception" | ...
          "citation_coverage": float,
          "total_words": int,
          "sections_dropped": int,
          "paragraphs_dropped": int,
        }

    The function is fail-safe: an unexpected exception inside the
    section loop is caught + logged + folded into the rejection
    response. Never raises.

    ``facts`` is currently unused — accepted as a future hook for
    cross-checking that paragraph citations reference fact_ids that
    actually exist on the cluster (the orchestrator plans to gate on
    "unknown fact_id citations" in a follow-up change).
    """
    sections_in = sections if isinstance(sections, list) else []
    paragraphs_dropped = [0]
    sections_dropped = 0
    cleaned: list[dict[str, Any]] = []

    # ── RAW citation-coverage gate (H-2) ─────────────────────────────
    # Compute citation coverage on the RAW input BEFORE per-paragraph
    # filtering. The earlier shape of this pass dropped uncited
    # paragraphs first; the surviving set was 100% cited and the gate
    # never fired — making the spec's ≥80% threshold dead code. Now
    # the gate is computed on the LLM's pre-filter output: if the
    # writer is producing too many uncited paragraphs to begin with,
    # we reject the article wholesale and the orchestrator falls back
    # to module-only rendering.
    total_input_paragraphs = 0
    cited_input_paragraphs = 0
    for s in sections_in:
        if not isinstance(s, dict):
            continue
        paragraphs = s.get("paragraphs")
        if not isinstance(paragraphs, list):
            continue
        for p in paragraphs:
            if not isinstance(p, dict):
                continue
            total_input_paragraphs += 1
            citations_raw = p.get("citations")
            if isinstance(citations_raw, list) and any(
                isinstance(c, str) and c.strip() for c in citations_raw
            ):
                cited_input_paragraphs += 1
    raw_coverage = (
        cited_input_paragraphs / total_input_paragraphs if total_input_paragraphs > 0 else 0.0
    )
    if total_input_paragraphs < 5:
        threshold = _CITATION_COVERAGE_GATE_SMALL
        tier = "small"
    elif total_input_paragraphs <= 10:
        threshold = _CITATION_COVERAGE_GATE_MID
        tier = "mid"
    else:
        threshold = _CITATION_COVERAGE_GATE
        tier = "large"
    logger.info(
        "narrative_validator: coverage_gate threshold=%.2f tier=%s paragraphs=%d coverage=%.2f",
        threshold,
        tier,
        total_input_paragraphs,
        raw_coverage,
    )
    if total_input_paragraphs > 0 and raw_coverage < threshold:
        logger.info(
            "narrative_article_rejected reason=low_citation_coverage "
            "raw_coverage=%.3f gate=%.2f cited=%d total=%d",
            raw_coverage,
            threshold,
            cited_input_paragraphs,
            total_input_paragraphs,
        )
        return [], {
            "rejected": True,
            "reason": "low_citation_coverage",
            "citation_coverage": raw_coverage,
            "total_words": 0,
            "sections_dropped": 0,
            "paragraphs_dropped": 0,
        }

    try:
        for idx, raw in enumerate(sections_in, start=1):
            section = _validate_section(
                raw,
                paragraphs_dropped=paragraphs_dropped,
                section_idx=idx,
            )
            if section is None:
                sections_dropped += 1
                continue
            cleaned.append(section)
    except Exception as exc:  # noqa: BLE001 — never propagate validator faults
        logger.exception(
            "narrative_validator_exception exc=%s — falling back to module-only",
            exc,
        )
        return [], {
            "rejected": True,
            "reason": "validator_exception",
            "citation_coverage": 0.0,
            "total_words": 0,
            "sections_dropped": sections_dropped,
            "paragraphs_dropped": paragraphs_dropped[0],
        }

    # Article-level metrics.
    total_words = 0
    total_paragraphs = 0
    paragraphs_with_citations = 0
    distinct_facts: set[str] = set()
    for section in cleaned:
        for p in section["paragraphs"]:
            total_paragraphs += 1
            total_words += _word_count(p["text"])
            if p["citations"]:
                paragraphs_with_citations += 1
                for c in p["citations"]:
                    distinct_facts.add(c)

    article_coverage = paragraphs_with_citations / total_paragraphs if total_paragraphs else 0.0

    # Article-over-cap soft warning. Only egregious bloat (> hard max)
    # triggers a rejection so landmark pages (channel overview) can
    # legitimately exceed the typical cap.
    if total_words > _ARTICLE_HARD_MAX_WORDS:
        logger.warning(
            "narrative_article_over_hard_cap words=%d hard_max=%d — rejecting",
            total_words,
            _ARTICLE_HARD_MAX_WORDS,
        )
        return [], {
            "rejected": True,
            "reason": "article_over_hard_cap",
            "citation_coverage": article_coverage,
            "total_words": total_words,
            "sections_dropped": sections_dropped,
            "paragraphs_dropped": paragraphs_dropped[0],
        }
    if total_words > _ARTICLE_TYPICAL_MAX_WORDS:
        logger.info(
            "narrative_article_over_cap words=%d typical_max=%d (soft cap)",
            total_words,
            _ARTICLE_TYPICAL_MAX_WORDS,
        )

    # Citation-coverage gate — hard fail when below 80%. This is the
    # primary safety net against hallucinated paragraphs slipping
    # through the per-paragraph filter.
    if total_paragraphs > 0 and article_coverage < _CITATION_COVERAGE_GATE:
        logger.info(
            "narrative_article_rejected reason=low_citation_coverage coverage=%.3f gate=%.2f",
            article_coverage,
            _CITATION_COVERAGE_GATE,
        )
        return [], {
            "rejected": True,
            "reason": "low_citation_coverage",
            "citation_coverage": article_coverage,
            "total_words": total_words,
            "sections_dropped": sections_dropped,
            "paragraphs_dropped": paragraphs_dropped[0],
        }

    # Empty result — no sections survived. Treat as rejection so the
    # orchestrator's persistence path stays consistent.
    if not cleaned:
        return [], {
            "rejected": True,
            "reason": "no_sections_after_validation",
            "citation_coverage": 0.0,
            "total_words": 0,
            "sections_dropped": sections_dropped,
            "paragraphs_dropped": paragraphs_dropped[0],
        }

    return cleaned, {
        "rejected": False,
        "reason": "ok",
        "citation_coverage": article_coverage,
        "total_words": total_words,
        "sections_dropped": sections_dropped,
        "paragraphs_dropped": paragraphs_dropped[0],
        "section_count": len(cleaned),
        "distinct_facts_cited": len(distinct_facts),
    }


__all__ = ["validate_narrative_sections"]
