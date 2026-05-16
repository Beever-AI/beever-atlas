"""Tests for verb normalization (PR-B).

Covers (v2 plan §B.6 + critique B-5, B-6):
- 12 canonical verbs map to identity.
- ~15 literal-table verbs map to the right canonical (direction preserved).
- 5 regex-bucket verbs land in the right bucket (DISCUSSES before ACTS_ON).
- 2 fallback-MENTIONS verbs hit the catch-all.
- ``REFERENCES_MEDIA`` is identity (defensive guard).
- ``BLOCKS`` and ``OWNED_BY`` are canonical (direction-flip rule
  enforced).
- Direction (source, target) is preserved across every literal mapping.
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.ingestion.verb_normalizer import (
    BUCKET_PATTERNS,
    CANONICAL_VERBS,
    VERB_NORMALIZATION,
    NormalizationLog,
    normalize_relationships,
    normalize_verb,
    summarize_normalizations,
)
from beever_atlas.models import GraphRelationship


# ---------------------------------------------------------------------------
# normalize_verb — single-input cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "DECIDED",
        "WORKS_ON",
        "USES",
        "OWNS",
        "OWNED_BY",  # promoted to canonical (B-5)
        "BLOCKS",  # promoted to canonical (B-5)
        "BLOCKED_BY",
        "DEPENDS_ON",
        "PART_OF",
        "MENTIONS",
        "DISCUSSES",
        "ASKS",
    ],
)
def test_canonical_verbs_pass_through_identity(raw: str) -> None:
    """All canonical verbs are returned unchanged with rule=identity."""
    canonical, rule = normalize_verb(raw)
    assert canonical == raw
    assert rule == "identity"
    assert raw in CANONICAL_VERBS


def test_references_media_is_identity() -> None:
    """REFERENCES_MEDIA bypasses the normalizer defensively (the
    real path that emits it never enters the normalizer at all, but
    the guard protects against future refactors)."""
    canonical, rule = normalize_verb("REFERENCES_MEDIA")
    assert canonical == "REFERENCES_MEDIA"
    assert rule == "identity"


@pytest.mark.parametrize(
    "raw,expected_canonical",
    [
        # USES family
        ("INTENDS_TO_USE", "USES"),
        ("PLANS_TO_USE", "USES"),
        ("ADOPTED", "USES"),
        ("MIGRATING_TO", "USES"),
        # CREATED family
        ("BUILT", "CREATED"),
        ("AUTHORED", "CREATED"),
        ("WROTE", "CREATED"),
        # DEPLOYED family
        ("SHIPPED", "DEPLOYED"),
        ("RELEASED", "DEPLOYED"),
        # REVIEWED family
        ("APPROVED", "REVIEWED"),
        ("REJECTED", "REVIEWED"),
        # PART_OF family
        ("MEMBER_OF", "PART_OF"),
        ("BELONGS_TO", "PART_OF"),
        # DEPENDS_ON family
        ("DEPENDS_UPON", "DEPENDS_ON"),
        ("RELIES_ON", "DEPENDS_ON"),
        # SHARES / SUGGESTS communication
        ("POSTED", "SHARES"),
        ("RECOMMENDED", "SUGGESTS"),
        ("PROPOSED", "SUGGESTS"),
        # ADVISES_AGAINST polarity-aware
        ("SUGGESTS_NOT_DISCLOSING", "ADVISES_AGAINST"),
        ("ADVISES_NOT_TO", "ADVISES_AGAINST"),
        # RESPONSIBLE_FOR (assigned_to direction-preserving)
        ("ASSIGNED_TO", "RESPONSIBLE_FOR"),
        # MENTIONS family
        ("REFERENCES", "MENTIONS"),
        # DISCUSSES family literal
        ("TALKED_ABOUT", "DISCUSSES"),
        ("BROUGHT_UP", "DISCUSSES"),
    ],
)
def test_literal_mappings(raw: str, expected_canonical: str) -> None:
    """Every literal-table entry produces the expected canonical verb
    with rule=literal:<raw>."""
    canonical, rule = normalize_verb(raw)
    assert canonical == expected_canonical
    assert rule == f"literal:{raw}"


def test_discusses_bucket_wins_over_acts_on() -> None:
    """PLANS_TO_DISCUSS_X must land in DISCUSSES (first-match wins;
    ACTS_ON would also match the PLANS_TO_ prefix)."""
    canonical, rule = normalize_verb("PLANS_TO_DISCUSS_PRICING")
    assert canonical == "DISCUSSES"
    assert rule == "regex:DISCUSSES"


def test_acts_on_bucket_catches_intent_verbs() -> None:
    """Verbs that match the ACTS_ON regex but not DISCUSSES land in
    ACTS_ON."""
    canonical, rule = normalize_verb("WILL_REVIEW_PROPOSAL")
    assert canonical == "ACTS_ON"
    assert rule == "regex:ACTS_ON"


def test_acts_on_compound_by_to_with_from() -> None:
    """Long-tail compounds like ``UPDATES_STATUS`` land in ACTS_ON."""
    canonical, rule = normalize_verb("UPDATES_STATUS")
    assert canonical == "ACTS_ON"
    assert rule == "regex:ACTS_ON"


def test_random_verb_falls_to_mentions() -> None:
    """A verb that matches no literal and no semantic bucket lands in
    MENTIONS via the catch-all."""
    canonical, rule = normalize_verb("RANDOM_VERB_XYZ")
    assert canonical == "MENTIONS"
    assert rule == "fallback:MENTIONS"


def test_empty_verb_falls_to_mentions() -> None:
    """Defensive: empty string also lands in MENTIONS."""
    canonical, rule = normalize_verb("")
    assert canonical == "MENTIONS"
    assert rule == "fallback:MENTIONS"


# ---------------------------------------------------------------------------
# Direction-preservation invariant (B-5 fix)
# ---------------------------------------------------------------------------


def test_every_literal_mapping_preserves_direction() -> None:
    """For every literal mapping, applying the rule to a fake
    relationship leaves (source, target) untouched. This protects
    against the v1 plan's BLOCKS→BLOCKED_BY direction flip.
    """
    for raw, _canonical in VERB_NORMALIZATION.items():
        rel = GraphRelationship(type=raw, source="Alice", target="Bob")
        out, _log = normalize_relationships([rel])
        assert len(out) == 1
        assert out[0].source == "Alice", f"source flipped for {raw}"
        assert out[0].target == "Bob", f"target flipped for {raw}"


def test_blocks_stays_blocks_owned_by_stays_owned_by() -> None:
    """Explicit regression for the v1 BLOCKS→BLOCKED_BY direction-flip
    bug.  Both verbs are now canonical."""
    canonical_blocks, _ = normalize_verb("BLOCKS")
    canonical_owned_by, _ = normalize_verb("OWNED_BY")
    assert canonical_blocks == "BLOCKS"
    assert canonical_owned_by == "OWNED_BY"


# ---------------------------------------------------------------------------
# normalize_relationships — batch path
# ---------------------------------------------------------------------------


def test_normalize_relationships_mutates_in_place_and_logs() -> None:
    """The batch path mutates ``rel.type`` and returns one ledger row
    per (raw, canonical, rule) tuple seen."""
    rels = [
        GraphRelationship(type="PLANS_TO_USE", source="A", target="B"),
        GraphRelationship(type="PLANS_TO_USE", source="A", target="C"),  # duplicate
        GraphRelationship(type="BUILT", source="D", target="E"),
        GraphRelationship(type="USES", source="F", target="G"),  # identity, no log
    ]
    out, log = normalize_relationships(rels, sync_job_id="job-1")
    # All types mutated correctly
    assert [r.type for r in out] == ["USES", "USES", "CREATED", "USES"]
    # Log has 2 unique entries (PLANS_TO_USE + BUILT). Identity rows
    # produce no log.
    log_keys = {(row.raw_verb, row.canonical_verb, row.rule) for row in log}
    assert ("PLANS_TO_USE", "USES", "literal:PLANS_TO_USE") in log_keys
    assert ("BUILT", "CREATED", "literal:BUILT") in log_keys
    assert len(log_keys) == 2


def test_summarize_normalizations_counts_duplicates() -> None:
    """When the same mapping appears multiple times in the ledger
    (e.g. 200 PLANS_TO_USE edges in one sync), the roll-up rolls them
    up into one row with the correct count."""
    log = [
        NormalizationLog(
            raw_verb="PLANS_TO_USE", canonical_verb="USES", rule="literal:PLANS_TO_USE"
        ),
        NormalizationLog(
            raw_verb="PLANS_TO_USE", canonical_verb="USES", rule="literal:PLANS_TO_USE"
        ),
        NormalizationLog(
            raw_verb="PLANS_TO_USE", canonical_verb="USES", rule="literal:PLANS_TO_USE"
        ),
        NormalizationLog(raw_verb="BUILT", canonical_verb="CREATED", rule="literal:BUILT"),
    ]
    rows = summarize_normalizations(log)
    by_raw = {r["original_verb"]: r for r in rows}
    assert by_raw["PLANS_TO_USE"]["count"] == 3
    assert by_raw["BUILT"]["count"] == 1


# ---------------------------------------------------------------------------
# Bucket-ordering invariant
# ---------------------------------------------------------------------------


def test_bucket_patterns_ordered_discusses_before_acts_on() -> None:
    """Structural assertion: BUCKET_PATTERNS must order DISCUSSES
    before ACTS_ON, otherwise PLANS_TO_DISCUSS_X would mis-route."""
    bucket_order = [bucket for _, bucket in BUCKET_PATTERNS]
    discusses_idx = bucket_order.index("DISCUSSES")
    acts_on_idx = bucket_order.index("ACTS_ON")
    mentions_idx = bucket_order.index("MENTIONS")
    assert discusses_idx < acts_on_idx < mentions_idx
