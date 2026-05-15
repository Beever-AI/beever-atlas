"""``acronym_legend`` module — frontend renderer.

Pulls glossary terms that ACTUALLY appear in this page's facts and
renders a compact two-column legend at the bottom of the page so
readers (and LLM agents reading the wiki) can resolve unfamiliar
acronyms in-place rather than jumping to the channel-wide glossary
page.

Source data: the channel's ``glossary_terms`` (list of dicts with
``term``, ``definition``, ``first_mentioned_by``, ...). The
orchestrator passes this in via ``compute_signals`` so signals +
data extraction agree on which terms count.

Renderer lives in
``web/src/components/wiki/modules/AcronymLegendModule.tsx`` —
this file is purely a builder.
"""

from __future__ import annotations

import re
from typing import Any


def _normalize_term(term: Any) -> str:
    """Coerce a glossary entry's ``term`` field to a clean string.
    Empty / non-string returns ``""``."""
    if not term:
        return ""
    return str(term).strip()


def _term_pattern(term: str) -> re.Pattern[str] | None:
    """Build a word-boundary regex for the term. ALL-CAPS acronyms
    use case-sensitive matching (so ``MFA`` matches ``MFA`` but not
    ``Mfa``); other terms allow case-insensitive matching.

    Returns ``None`` for terms that contain no word characters
    (would produce a regex that matches everything).
    """
    if not term:
        return None
    escaped = re.escape(term)
    # Heuristic: if the term is all-caps + alphanumeric (typical
    # acronym shape — MFA, SAML, OIDC), keep case sensitivity so we
    # don't false-match common English words. Otherwise lowercase
    # match (e.g., a term like "wiki compiler" should match
    # "Wiki Compiler" too).
    is_acronym = term.isupper() and term.replace(" ", "").isalnum() and len(term) >= 2
    flags = 0 if is_acronym else re.IGNORECASE
    try:
        return re.compile(rf"\b{escaped}\b", flags)
    except re.error:
        return None


def count_glossary_terms_used(
    glossary: list[dict] | None,
    facts: list[dict] | None,
) -> int:
    """Count distinct glossary terms whose pattern matches any fact's
    ``memory_text``. Used by ``compute_signals`` to populate
    ``glossary_terms_used``."""
    if not isinstance(glossary, list) or not isinstance(facts, list):
        return 0
    if not glossary or not facts:
        return 0

    # Collect fact bodies once; the regex match is the hot loop.
    # Strip safety wrappers so they don't influence matching (a tag
    # like ``<untrusted>`` won't be caught by ``\bTERM\b`` anyway,
    # but stripping keeps the bodies consistent with what the rest
    # of the pipeline sees).
    from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

    bodies: list[str] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        body = _strip_safety_markers(f.get("memory_text") or f.get("fact") or f.get("text") or "")
        if body:
            bodies.append(body)
    if not bodies:
        return 0

    hits = 0
    for entry in glossary:
        if isinstance(entry, dict):
            term = _normalize_term(entry.get("term"))
        elif isinstance(entry, str):
            term = _normalize_term(entry)
        else:
            term = ""
        pat = _term_pattern(term)
        if pat is None:
            continue
        if any(pat.search(b) for b in bodies):
            hits += 1
    return hits


def _collect_structured_terms(facts: list[dict]) -> set[str]:
    """Phase 3 — pull structured ``glossary_terms`` off each fact.

    The fact extractor populates ``glossary_terms`` with candidate
    acronyms / domain terms appearing in the fact body. Returns a
    set of stripped term strings; empty when no fact carries any.
    Used by ``build_acronym_legend_data`` as the structured-first
    path before falling back to regex scanning.
    """
    out: set[str] = set()
    for f in facts:
        if not isinstance(f, dict):
            continue
        terms = f.get("glossary_terms")
        if not isinstance(terms, list):
            continue
        for t in terms:
            if isinstance(t, str):
                cleaned = t.strip()
                if cleaned:
                    out.add(cleaned)
    return out


def build_acronym_legend_data(
    glossary: list[dict] | None,
    facts: list[dict] | None,
) -> dict[str, Any]:
    """Build the payload the React AcronymLegendModule consumes.

    Phase 3 — when any fact carries structured ``glossary_terms``,
    filter the channel glossary against THAT set (case-sensitive for
    acronyms, case-insensitive for phrases — same ALL-CAPS heuristic
    as the regex path). When no fact carries structured terms, fall
    back to regex word-boundary scanning of fact bodies.

    Cap returned items at 30 — beyond that the legend stops being a
    reading aid and starts competing with the glossary page.

    Returns:
        {
          "label": "Terms used on this page",
          "renderer_kind": "frontend",
          "items": [
            {"term": "MFA", "definition": "Multi-Factor Authentication", "first_mentioned_by": "Dante Lok"},
            ...
          ]
        }
    """
    if not isinstance(glossary, list) or not isinstance(facts, list):
        return {
            "label": "Terms used on this page",
            "renderer_kind": "frontend",
            "items": [],
        }

    from beever_atlas.wiki.modules._text_utils import _strip_safety_markers

    # ---- Phase 3 structured-first path -----------------------------
    structured_terms = _collect_structured_terms([f for f in facts if isinstance(f, dict)])

    items: list[dict[str, Any]] = []
    seen_terms: set[str] = set()

    if structured_terms:
        # Build a normalized lookup: ALL-CAPS acronyms match case-
        # sensitively; other terms case-insensitively. This mirrors
        # the regex path's ``_term_pattern`` policy so structured
        # vs fallback yield the same legend for the same content.
        structured_acronyms = {t for t in structured_terms if t.isupper()}
        structured_lc = {t.lower() for t in structured_terms if not t.isupper()}
        for entry in glossary:
            if isinstance(entry, dict):
                term = _normalize_term(entry.get("term"))
                definition = str(entry.get("definition") or "").strip()
                first_mentioned = str(
                    entry.get("first_mentioned_by") or entry.get("author") or ""
                ).strip()
            elif isinstance(entry, str):
                term = _normalize_term(entry)
                definition = ""
                first_mentioned = ""
            else:
                continue
            if not term or term.lower() in seen_terms:
                continue
            is_acronym = term.isupper() and term.replace(" ", "").isalnum() and len(term) >= 2
            matched = term in structured_acronyms if is_acronym else term.lower() in structured_lc
            if not matched:
                continue
            seen_terms.add(term.lower())
            items.append(
                {
                    "term": term,
                    "definition": definition,
                    "first_mentioned_by": first_mentioned,
                }
            )
            if len(items) >= 30:
                break
        return {
            "label": "Terms used on this page",
            "renderer_kind": "frontend",
            "items": items,
        }

    # ---- Legacy regex fallback -------------------------------------
    bodies: list[str] = []
    for f in facts:
        if not isinstance(f, dict):
            continue
        body = _strip_safety_markers(f.get("memory_text") or f.get("fact") or f.get("text") or "")
        if body:
            bodies.append(body)

    for entry in glossary:
        if isinstance(entry, dict):
            term = _normalize_term(entry.get("term"))
            definition = str(entry.get("definition") or "").strip()
            first_mentioned = str(
                entry.get("first_mentioned_by") or entry.get("author") or ""
            ).strip()
        elif isinstance(entry, str):
            term = _normalize_term(entry)
            definition = ""
            first_mentioned = ""
        else:
            continue
        if not term or term.lower() in seen_terms:
            continue
        pat = _term_pattern(term)
        if pat is None:
            continue
        if not bodies or not any(pat.search(b) for b in bodies):
            continue
        seen_terms.add(term.lower())
        items.append(
            {
                "term": term,
                "definition": definition,
                "first_mentioned_by": first_mentioned,
            }
        )
        if len(items) >= 30:
            break

    return {
        "label": "Terms used on this page",
        "renderer_kind": "frontend",
        "items": items,
    }
