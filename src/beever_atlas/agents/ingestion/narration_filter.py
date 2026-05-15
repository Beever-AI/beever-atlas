"""Post-extraction narration filter.

Detects activity-log narration in extracted fact text and either
rewrites it (when a clean rewrite is mechanical) or flags the fact
as low-quality. This is the defense-in-depth layer behind the
fact-extractor prompt's writing-style discipline.

Activity-log narration looks like::

    "Thomas Chong shared a link to the GitHub repository for Ory Hydra"
    "Jacky Chan mentioned that fastapi-sso could be useful"

The underlying knowledge — *what* the linked resource is, *what* the
mentioned claim says — is what callers want stored in
``memory_text``. The author is preserved separately on the fact's
``author_name`` field, so we strip the "<Name> shared/noted/..."
opener and keep the residual knowledge.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Word-boundary regex for activity-log phrases. Matches case-insensitively.
# The "shared a..." branch allows an optional adjective ("Neo4j", "GitHub")
# between the article and the noun so phrases like "shared a Neo4j blog post"
# trip the detector.
_ACTIVITY_LOG_RE = re.compile(
    r"\b(?:"
    r"shared\s+(?:a|an)(?:\s+[A-Za-z][A-Za-z0-9_\-]*){0,2}"
    r"\s+(?:link|article|repository|blog\s+post|post|file|document)|"
    r"noted\s+that|"
    r"mentioned\s+that|"
    r"posted\s+about|"
    r"presented\s+that|"
    r"asked\s+(?:the\s+team|about)"
    r")\b",
    re.IGNORECASE,
)

# Common opener patterns: "Thomas Chong shared a link..." → strip the
# author attribution prefix so the residual text can be rewritten.
_AUTHOR_PREFIX_RE = re.compile(
    r"^([A-Z][a-zA-Z\s\-\.']{1,40})\s+(shared|noted|mentioned|posted|presented|asked)\b",
)


def detect_activity_narration(text: str) -> str | None:
    """Return the matched activity-narration phrase, or None when clean.

    Used by callers to decide whether to attempt a rewrite or flag the
    fact for downstream quality reduction.
    """
    if not text:
        return None
    match = _ACTIVITY_LOG_RE.search(text)
    return match.group(0) if match else None


def attempt_rewrite(text: str, author_name: str = "") -> tuple[str, bool]:
    """Best-effort rewrite of activity-narration prose.

    Returns ``(rewritten_text, was_rewritten)``. When the rewrite is
    confident (mechanical strip of "<Author> shared a link to <X>" →
    "<X>"), returns the cleaner text. When rewrite would require
    semantic understanding (uncertain), returns the original text
    with ``was_rewritten=False`` so the caller can flag the fact for
    quality demotion.
    """
    if not text:
        return text, False

    # Strip author prefix if present
    author_stripped = text
    prefix_match = _AUTHOR_PREFIX_RE.match(text)
    if prefix_match:
        # Drop "<Name> " opener so the verb starts the residual string
        author_stripped = text[prefix_match.start(2) :]

    # Pattern 1: "shared a link to <X>" / "shared a repository for <X>" /
    #            "shared a Neo4j blog post titled <X>"
    #            → "<X>" (drop the share-act, keep the linked resource).
    #            Allows up to two adjective tokens before the noun.
    pattern1 = re.match(
        r"^shared\s+(?:a|an)(?:\s+[A-Za-z][A-Za-z0-9_\-]*){0,2}"
        r"\s+(?:link|article|repository|blog\s+post|post|file|document)"
        r"\s+(?:to|for|titled|about|on)\s+(.+)$",
        author_stripped,
        re.IGNORECASE,
    )
    if pattern1:
        residual = pattern1.group(1).strip()
        residual = _capitalise_first(residual)
        return residual, True

    # Pattern 2: "noted that <X>" / "mentioned that <X>"
    #            → "<X>" (drop the "noted that" verb)
    pattern2 = re.match(
        r"^(?:noted|mentioned|stated|pointed\s+out|highlighted)\s+that\s+(.+)$",
        author_stripped,
        re.IGNORECASE,
    )
    if pattern2:
        residual = pattern2.group(1).strip()
        residual = _capitalise_first(residual)
        return residual, True

    # No confident rewrite available
    return text, False


def _capitalise_first(text: str) -> str:
    """Capitalise the first character of ``text`` unless its first word
    looks like a proper-noun lowercase identifier (e.g. ``fastapi-sso``,
    ``google-auth-oauthlib``). Hyphenated all-lowercase tokens are
    common package names and should be preserved verbatim.
    """
    if not text:
        return text
    first_word = text.split()[0]
    # Preserve identifiers like "fastapi-sso" — hyphen + all-lower
    if "-" in first_word and first_word.islower():
        return text
    if text[0].islower():
        return text[0].upper() + text[1:]
    return text


def filter_fact(fact: dict[str, Any]) -> dict[str, Any]:
    """Apply narration filtering to a fact dict.

    Returns a new dict with potentially-cleaned ``memory_text``. Logs
    structured telemetry for each detection. When rewrite is not
    confident, demotes the fact's ``importance`` to ``"low"`` so
    downstream consumers (key_facts table sort, decision_banner picker)
    deprioritize it.

    Idempotent: passing a clean fact returns it unchanged.
    """
    text = fact.get("memory_text", "")
    if not isinstance(text, str):
        return fact

    detected = detect_activity_narration(text)
    if not detected:
        return fact

    # Detected — try rewrite
    author = fact.get("author_name", "") or ""
    rewritten, was_rewritten = attempt_rewrite(text, author_name=author)

    fact_id = fact.get("fact_id") or fact.get("id") or "<unknown>"
    if was_rewritten:
        logger.info(
            "fact_narration_rewritten fact_id=%s phrase=%s before=%r after=%r",
            fact_id,
            detected,
            text[:80],
            rewritten[:80],
        )
        new_fact = dict(fact)
        new_fact["memory_text"] = rewritten
        return new_fact

    # Rewrite not confident — demote importance, log
    logger.warning(
        "fact_narration_demoted fact_id=%s phrase=%s text=%r",
        fact_id,
        detected,
        text[:120],
    )
    new_fact = dict(fact)
    new_fact["importance"] = "low"
    return new_fact


def filter_facts(facts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Apply narration filtering to a list of facts. Pure function.

    Fail-safe: any unexpected exception falls back to the original
    list so the extraction pipeline never crashes on a regex edge
    case.
    """
    try:
        return [filter_fact(f) for f in facts if isinstance(f, dict)]
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.exception("narration_filter_failed err=%s; falling back to original facts", exc)
        return list(facts)
