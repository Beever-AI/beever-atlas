"""Verb normalization for graph relationships.

Maps the long tail of LLM-emitted SCREAMING_SNAKE_CASE verbs to a small
canonical set BEFORE the relationship is written to Neo4j. The mutation
happens inside ``persister._upsert_graph`` so the forward write path
sees the clean verb shape and downstream consumers (graph API, wiki
linker, cytoscape edge labels, MCP tools) need no read-time fixes.

Design highlights (v2 plan §B):
- ``REFERENCES_MEDIA`` flows through a separate batch_upsert_media path
  and never reaches the normalizer — but we keep the identity guard
  defensively.
- Direction is NEVER flipped. Both ``BLOCKS`` and ``BLOCKED_BY`` are
  canonical; ditto ``OWNS`` and ``OWNED_BY``. The plan's v1 inversion
  was withdrawn.
- Per-edge ``original_verb`` and ``normalization_rule`` properties are
  NOT persisted (would require model + Cypher + projection changes).
  Instead a structured logger emits one line per normalization, and
  the persister surfaces a ``NormalizationLog`` ledger that BatchProcessor
  rolls into ``sync_summary.normalizations[]`` for replay-based recovery.
- Bucket regex is ordered first-match-wins. DISCUSSES checked before
  ACTS_ON; MENTIONS is the fallback catch-all.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass

from beever_atlas.models import GraphRelationship

logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------
# Canonical verb set
# ----------------------------------------------------------------------

# 12 from entity_extractor.py:103-104 + 6 communication verbs (per v2 §B.3.1)
# + BLOCKS + OWNED_BY (promoted to canonical per B-5 critique)
# + ADVISES_AGAINST (new polarity-aware canonical per B.10 fold)
# = 21 canonical verbs.
CANONICAL_VERBS: frozenset[str] = frozenset(
    {
        # Action / decision (entity_extractor preferred types)
        "DECIDED",
        "WORKS_ON",
        "USES",
        "OWNS",
        "OWNED_BY",  # promoted (B-5 fix)
        "BLOCKED_BY",
        "BLOCKS",  # promoted (B-5 fix)
        "DEPENDS_ON",
        "CREATED",
        "REVIEWED",
        "DEPLOYED",
        "PARTICIPATES_IN",
        "RESPONSIBLE_FOR",
        "PART_OF",
        # Communication verbs (user-specified)
        "MENTIONS",
        "DISCUSSES",
        "ASKS",
        "SUGGESTS",
        "SHARES",
        # Polarity-aware
        "ADVISES_AGAINST",
        # System-emitted (defensive only — never normalized)
        "REFERENCES_MEDIA",
        # Bucket terminal (catch-all targets)
        "ACTS_ON",
    }
)


# ----------------------------------------------------------------------
# Literal seed mappings — explicit verb → canonical verb
# Direction is PRESERVED for every mapping (B-5 invariant; tested).
# ----------------------------------------------------------------------

VERB_NORMALIZATION: dict[str, str] = {
    # USES family
    "INTENDS_TO_USE": "USES",
    "PLANS_TO_USE": "USES",
    "SUGGESTS_TO_USE": "USES",
    "MIGRATING_TO": "USES",
    "ADOPTED": "USES",
    # ADVISES_AGAINST (polarity-flipped suggestions)
    "SUGGESTS_NOT_DISCLOSING": "ADVISES_AGAINST",
    "ADVISES_NOT_TO": "ADVISES_AGAINST",
    "RECOMMENDS_AGAINST": "ADVISES_AGAINST",
    # PARTICIPATES_IN family
    "PLANS_TO_NEGOTIATE_WITH": "PARTICIPATES_IN",
    "SUGGESTS_TO_SCHEDULE_MEETING_WITH": "PARTICIPATES_IN",
    "MET_WITH": "PARTICIPATES_IN",
    "ATTENDED": "PARTICIPATES_IN",
    # ASKS family
    "ASKS_PERMISSION_TO_SHARE_WITH": "ASKS",
    "QUESTIONS": "ASKS",
    "INQUIRES": "ASKS",
    # Direction-preserving responsibility
    "ASSIGNED_TO": "RESPONSIBLE_FOR",  # A assigned to B → A responsible for B
    "ACCOUNTABLE_FOR": "RESPONSIBLE_FOR",
    # DEPENDS_ON family
    "DEPENDS_UPON": "DEPENDS_ON",
    "RELIES_ON": "DEPENDS_ON",
    "REQUIRES": "DEPENDS_ON",
    # PART_OF family
    "MEMBER_OF": "PART_OF",
    "BELONGS_TO": "PART_OF",
    "CONTAINED_IN": "PART_OF",
    # CREATED family
    "BUILT": "CREATED",
    "AUTHORED": "CREATED",
    "WROTE": "CREATED",
    "DESIGNED": "CREATED",
    # DEPLOYED family
    "SHIPPED": "DEPLOYED",
    "RELEASED": "DEPLOYED",
    "LAUNCHED": "DEPLOYED",
    "ROLLED_OUT": "DEPLOYED",
    # REVIEWED family
    "REVIEWS": "REVIEWED",
    "APPROVED": "REVIEWED",
    "REJECTED": "REVIEWED",  # see B.10.2 — fold for v2, promote later
    "FEEDBACK_ON": "REVIEWED",
    # MENTIONS / DISCUSSES / SHARES / SUGGESTS communication family
    "REFERENCES": "MENTIONS",
    "TALKED_ABOUT": "DISCUSSES",
    "BROUGHT_UP": "DISCUSSES",
    "RAISED": "DISCUSSES",
    "SHARED_WITH": "SHARES",
    "POSTED": "SHARES",
    "RECOMMENDED": "SUGGESTS",
    "PROPOSED": "SUGGESTS",
}


# ----------------------------------------------------------------------
# Bucket regex — ordered first-match-wins (B-6 fix)
# DISCUSSES checked BEFORE ACTS_ON so ``PLANS_TO_DISCUSS_X`` lands in
# DISCUSSES, not in ACTS_ON's intent bucket. MENTIONS is the catch-all.
# ----------------------------------------------------------------------

BUCKET_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # DISCUSSES (talked-about family) — most specific first.  Matches
    # the verb root ``DISCUSS`` (covers ``DISCUSS``, ``DISCUSSES``,
    # ``DISCUSSED``, ``PLANS_TO_DISCUSS_X``) as well as the other
    # explicit communication families.
    (
        re.compile(
            r"(?:^|_)(?:DISCUSS|TALKS_ABOUT|TALKED_ABOUT|RAISED_TOPIC|"
            r"BRINGS_UP|MENTIONS_TOPIC|REPLIES)"
        ),
        "DISCUSSES",
    ),
    # ACTS_ON — intent/plan verbs that aren't a canonical action,
    # plus generic update / modify / move / *_BY|TO|WITH|FROM compounds.
    # Anchored at the front; trailing form is unrestricted so
    # ``WILL_REVIEW_PROPOSAL`` lands here just as ``WILL_REVIEW`` does.
    (
        re.compile(
            r"^(?:PLANS_TO_|INTENDS_TO_|WILL_|WANTS_TO_|HOPES_TO_|"
            r"UPDATES_?|UPDATE_?|MODIFIES_?|MODIFY_?|"
            r"CREATES_?|CREATE_?|DELETES_?|DELETE_?|MOVES_?|MOVE_?)"
            r"|"
            r"^[A-Z]+_(?:BY|TO|WITH|FROM)$"
        ),
        "ACTS_ON",
    ),
    # MENTIONS — fallback catch-all (matches anything)
    (re.compile(r".*"), "MENTIONS"),
]


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class NormalizationLog:
    """One row per (raw_verb, canonical_verb, rule) per write.

    The persister rolls up duplicate rows into ``sync_summary.normalizations[]``
    entries keyed by ``(sync_job_id, raw, canonical, rule)`` with a count
    field. The ledger is the recovery path: an operator who needs to
    reverse a bad mapping replays from the sync_summary record.
    """

    raw_verb: str
    canonical_verb: str
    rule: str  # "identity" | "literal:<raw>" | "regex:<bucket>" | "fallback:MENTIONS"


def normalize_verb(raw: str) -> tuple[str, str]:
    """Return ``(canonical_verb, rule_string)`` for a raw verb.

    Resolution order:
      1. ``raw`` already canonical → identity.
      2. ``raw == "REFERENCES_MEDIA"`` → identity (defensive; the path
         that emits this never enters the normalizer, but the guard
         protects against future refactors).
      3. ``raw`` in literal table → ``literal:<raw>``.
      4. First bucket-regex match → ``regex:<bucket>`` (MENTIONS pattern
         is the catch-all, so we always return).
    """
    if not raw:
        return ("MENTIONS", "fallback:MENTIONS")
    if raw in CANONICAL_VERBS:
        return (raw, "identity")
    if raw == "REFERENCES_MEDIA":
        return (raw, "identity")
    if raw in VERB_NORMALIZATION:
        return (VERB_NORMALIZATION[raw], f"literal:{raw}")
    for pattern, bucket in BUCKET_PATTERNS:
        if pattern.search(raw):
            if bucket == "MENTIONS":
                # Catch-all hit; mark as fallback so the audit ledger can
                # distinguish "regex bucket landing" from "fell off the
                # end of the list".
                return (bucket, "fallback:MENTIONS")
            return (bucket, f"regex:{bucket}")
    # Unreachable — the final regex catches anything — but keep an
    # explicit return for defensive completeness.
    return ("MENTIONS", "fallback:MENTIONS")


def normalize_relationships(
    rels: list[GraphRelationship],
    *,
    sync_job_id: str = "",
) -> tuple[list[GraphRelationship], list[NormalizationLog]]:
    """Mutate ``rel.type`` in place to canonical form and return the
    audit ledger.

    The relationships list is returned as-is (same objects) so the
    persister can pass it straight to ``batch_upsert_relationships``.
    Only ``type`` is mutated; source/target/direction is preserved by
    construction (B-5 invariant).
    """
    log: list[NormalizationLog] = []
    counter: Counter[tuple[str, str, str]] = Counter()
    for rel in rels:
        raw = rel.type or ""
        canonical, rule = normalize_verb(raw)
        if canonical != raw:
            # Mutate in place — caller already owns the model objects.
            rel.type = canonical
            counter[(raw, canonical, rule)] += 1
            # Structured logger fires at write time (independent of the
            # sync_summary roll-up).  Keyword args go via ``extra=`` so
            # log aggregators can index them.
            logger.info(
                "verb_normalized",
                extra={
                    "original": raw,
                    "canonical": canonical,
                    "rule": rule,
                    "sync_job_id": sync_job_id,
                    "source": rel.source,
                    "target": rel.target,
                },
            )
    log.extend(
        NormalizationLog(raw_verb=raw, canonical_verb=canon, rule=rule)
        for (raw, canon, rule), _ in counter.items()
    )
    # Attach counts as a side return so the persister can include them
    # in the sync_summary ledger.
    return rels, log


def summarize_normalizations(rels_logs: list[NormalizationLog]) -> list[dict[str, str | int]]:
    """Collapse a list of ``NormalizationLog`` rows (across an entire
    sync) into a ``sync_summary.normalizations[]`` entry shape.

    Returns one dict per ``(original_verb, canonical, rule)`` tuple with
    a ``count`` of how many edges were affected.
    """
    counter: Counter[tuple[str, str, str]] = Counter()
    for row in rels_logs:
        counter[(row.raw_verb, row.canonical_verb, row.rule)] += 1
    return [
        {
            "original_verb": raw,
            "canonical": canon,
            "rule": rule,
            "count": count,
        }
        for (raw, canon, rule), count in counter.items()
    ]
