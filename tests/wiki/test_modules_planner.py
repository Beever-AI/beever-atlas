"""Tests for the planner support functions:
- ``compute_signals`` — pure aggregation of cluster data
- ``_validate_plan`` — drops modules whose eligibility fails

The orchestrator runs the actual LLM call (single unified prompt).
The end-to-end LLM-call flow is covered in
``test_modules_orchestrator.py``.
"""

from __future__ import annotations

from beever_atlas.wiki.modules.planner import (
    ModulePlan,
    _validate_plan,
    compute_signals,
)


# ---------------------------------------------------------------------------
# compute_signals — pure aggregation
# ---------------------------------------------------------------------------


def test_compute_signals_counts_facts_and_decisions() -> None:
    cluster = {
        "title": "Auth",
        "member_facts": [
            {"fact_type": "event", "date": "2026-04-01"},
            {"fact_type": "event", "date": "2026-04-15"},
            {"fact_type": "decision", "date": "2026-04-20"},
            {"fact_type": "claim"},
            {"fact_type": "claim"},
        ],
    }
    signals = compute_signals(
        cluster=cluster,
        decisions=[{"decision": "Adopt JWT"}, {"decision": "Drop SAML"}],
    )
    assert signals["fact_count"] == 5
    assert signals["decision_count"] == 2
    assert signals["event_count"] == 3  # event + event + decision (decision is event-typed)
    assert signals["event_span_days"] == 19  # 2026-04-01 to 2026-04-20


def test_compute_signals_buckets_media_by_type() -> None:
    media = [
        {"kind": "image", "url": "screenshot.png", "source_fact_id": "f1"},
        {"kind": "image", "url": "graph.png"},  # no source_fact_id → gallery
        {"url": "https://youtube.com/watch?v=x"},
        {"url": "https://example.com/doc.pdf"},
        {"url": "https://example.com/article"},  # generic link
    ]
    signals = compute_signals(cluster={"title": "T", "member_facts": []}, media=media)
    assert signals["inline_media_count"] == 1
    assert signals["gallery_media_count"] == 1
    assert signals["video_media_count"] == 1
    assert signals["pdf_media_count"] == 1
    assert signals["link_media_count"] == 1


def test_compute_signals_detects_hero_candidate() -> None:
    media = [
        {
            "kind": "image",
            "url": "dashboard.png",
            "alt": "Insights Dashboard",
            "referencing_fact_count": 4,
        }
    ]
    signals = compute_signals(
        cluster={"title": "Insights Dashboard", "member_facts": []}, media=media
    )
    assert signals["has_media_hero_candidate"] is True


def test_compute_signals_strong_claim_authors_distinct() -> None:
    facts = [
        {"fact_type": "opinion", "author_name": "Jacky"},
        {"fact_type": "opinion", "author_name": "Jacky"},
        {"fact_type": "claim", "author_name": "Thomas"},
        {"fact_type": "decision", "author_name": "Alan"},
        {"fact_type": "event", "author_name": "Pete"},  # not strong-claim
    ]
    signals = compute_signals(cluster={"title": "T", "member_facts": facts})
    assert signals["strong_claim_author_count"] == 3  # Jacky, Thomas, Alan


def test_compute_signals_max_edges_between_same_pair_finds_dominant_pair() -> None:
    relationships = [
        # 3 edges between (A, B) — all different verbs.
        {"from": "A", "to": "B", "label": "owns"},
        {"from": "A", "to": "B", "label": "supports"},
        {"from": "A", "to": "B", "label": "deprecates"},
        # 1 edge between (B, C).
        {"from": "B", "to": "C", "label": "supports"},
        # 1 edge between (A, C).
        {"from": "A", "to": "C", "label": "links"},
    ]
    signals = compute_signals(
        cluster={"title": "T", "member_facts": []}, relationships=relationships
    )
    assert signals["max_edges_between_same_pair"] == 3
    # Verbs: owns, supports, deprecates, links → 4 distinct.
    assert signals["distinct_edge_verbs"] == 4


def test_compute_signals_distinct_edge_verbs_counts_unique_labels() -> None:
    relationships = [
        {"from": "A", "to": "B", "label": "REFERS"},
        {"from": "A", "to": "C", "label": "REFERS"},
        {"from": "B", "to": "C", "label": "REFERS"},
    ]
    signals = compute_signals(
        cluster={"title": "T", "member_facts": []}, relationships=relationships
    )
    assert signals["distinct_edge_verbs"] == 1
    assert signals["max_edges_between_same_pair"] == 1


def test_compute_signals_distinct_edge_verbs_uses_type_field_when_label_missing() -> None:
    relationships = [
        {"from": "A", "to": "B", "type": "REFERS"},
        {"from": "A", "to": "C", "type": "ASSIGNED_ROLE"},
    ]
    signals = compute_signals(
        cluster={"title": "T", "member_facts": []}, relationships=relationships
    )
    assert signals["distinct_edge_verbs"] == 2


def test_compute_signals_edge_signals_handle_empty_relationships() -> None:
    signals = compute_signals(cluster={"title": "T", "member_facts": []}, relationships=[])
    assert signals["max_edges_between_same_pair"] == 0
    assert signals["distinct_edge_verbs"] == 0


def test_compute_signals_process_step_edge_count_counts_directed_edges() -> None:
    process_steps = [
        {"id": "s1", "label": "Start", "to": "s2"},
        {"id": "s2", "label": "Middle", "to": "s3"},
        {"id": "s3", "label": "End"},  # no `to` field → orphan
        {"id": "s4", "label": "Orphan"},  # no `to` field → orphan
    ]
    signals = compute_signals(
        cluster={"title": "T", "member_facts": []}, process_steps=process_steps
    )
    assert signals["process_step_edge_count"] == 2
    assert signals["process_step_count"] == 4


def test_compute_signals_process_step_edge_count_zero_when_all_orphans() -> None:
    process_steps = [{"id": "s1"}, {"id": "s2"}, {"id": "s3"}]
    signals = compute_signals(
        cluster={"title": "T", "member_facts": []}, process_steps=process_steps
    )
    assert signals["process_step_edge_count"] == 0


def test_compute_signals_handles_missing_dates_gracefully() -> None:
    cluster = {
        "title": "T",
        "member_facts": [
            {"fact_type": "event"},
            {"fact_type": "event", "date": "bogus"},
        ],
    }
    signals = compute_signals(cluster=cluster)
    assert signals["event_span_days"] == 0  # no parsable dates


# ---------------------------------------------------------------------------
# _validate_plan — drops invalid picks, dedups anchors, accepts media pins
# ---------------------------------------------------------------------------


def test_validate_plan_drops_unknown_module_ids() -> None:
    raw = {"modules": [{"id": "key_facts", "anchor": "kf"}, {"id": "bogus_module"}]}
    signals = {"fact_count": 10}
    plan = _validate_plan(raw, signals)
    ids = [m["id"] for m in plan.modules]
    assert ids == ["key_facts"]


def test_validate_plan_drops_modules_failing_eligibility() -> None:
    raw = {
        "modules": [
            {"id": "key_facts", "anchor": "kf"},  # needs fact_count ≥ 5
            {"id": "comparison_matrix", "anchor": "cm"},  # needs alternative_count ≥ 2
        ]
    }
    signals = {"fact_count": 10, "alternative_count": 0}
    plan = _validate_plan(raw, signals)
    ids = [m["id"] for m in plan.modules]
    assert "key_facts" in ids
    assert "comparison_matrix" not in ids


def test_validate_plan_dedups_anchors() -> None:
    raw = {
        "modules": [
            {"id": "key_facts", "anchor": "x"},
            {"id": "decision_log", "anchor": "x"},
        ]
    }
    signals = {"fact_count": 10, "decision_count": 3}
    plan = _validate_plan(raw, signals)
    anchors = [m["anchor"] for m in plan.modules]
    assert anchors == ["x", "x-2"]


def test_validate_plan_keeps_valid_media_pins() -> None:
    raw = {
        "modules": [{"id": "key_facts"}],
        "media_pins": [
            {"media_id": "m1", "fact_id": "f1", "slot": "inline"},
            {"media_id": "m2", "fact_id": "f2", "slot": "ghost_slot"},  # invalid
        ],
    }
    signals = {"fact_count": 10}
    plan = _validate_plan(raw, signals)
    assert len(plan.media_pins) == 1
    assert plan.media_pins[0].slot == "inline"


def test_validate_plan_handles_garbage_modules_field() -> None:
    raw = {"modules": "not a list"}
    plan = _validate_plan(raw, {"fact_count": 1})
    assert plan.modules == []


# ---------------------------------------------------------------------------
# ModulePlan dataclass
# ---------------------------------------------------------------------------


def test_module_plan_to_dict_round_trip() -> None:
    plan = ModulePlan(
        modules=[{"id": "key_facts", "anchor": "kf"}],
    )
    d = plan.to_dict()
    assert d["modules"] == [{"id": "key_facts", "anchor": "kf"}]
    assert d["media_pins"] == []


def test_module_plan_is_empty_predicate() -> None:
    assert ModulePlan().is_empty() is True
    assert ModulePlan(modules=[{"id": "x"}]).is_empty() is False


# ---------------------------------------------------------------------------
# Phase 4 tension detection — compute_signals wires the real detector
# in, so ``tension_count`` reflects opposing-sentiment opinion pairs
# sharing an entity tag (Phase 3 sentiment field is REQUIRED).
# ---------------------------------------------------------------------------


def test_compute_signals_tension_count_zero_when_no_sentiment() -> None:
    """Pre-Phase-3 facts (no sentiment field) cannot ever fire a
    tension — the detector skips facts whose sentiment is null. This
    guards the quality gate documented on AtomicFact.sentiment."""
    facts = [
        {"fact_type": "opinion", "author_name": "A", "entity_tags": ["X"]},
        {"fact_type": "opinion", "author_name": "B", "entity_tags": ["X"]},
    ]
    signals = compute_signals(cluster={"title": "T", "member_facts": facts})
    assert signals["tension_count"] == 0


def test_compute_signals_tension_count_one_when_pair_detected() -> None:
    """A 2-fact cluster with opposing sentiments + shared entity MUST
    surface as ``tension_count == 1``. This is the canonical case the
    Phase 4 detector activates."""
    facts = [
        {
            "id": "f1",
            "fact_type": "opinion",
            "author_name": "Jacky",
            "memory_text": "Custom is tuned for our pipeline.",
            "sentiment": "positive",
            "entity_tags": ["X"],
            "message_ts": "2026-04-22T10:00:00Z",
        },
        {
            "id": "f2",
            "fact_type": "opinion",
            "author_name": "Thomas",
            "memory_text": "Custom will rot — switch.",
            "sentiment": "concerning",
            "entity_tags": ["X"],
            "message_ts": "2026-04-23T10:00:00Z",
        },
    ]
    signals = compute_signals(cluster={"title": "T", "member_facts": facts})
    assert signals["tension_count"] == 1


def test_compute_signals_tension_count_zero_when_no_shared_entity() -> None:
    """Opposing sentiments on different subjects do NOT count — the
    heuristic requires shared entity overlap to avoid false positives
    across unrelated topics that just happened to be co-clustered."""
    facts = [
        {
            "id": "f1",
            "fact_type": "opinion",
            "sentiment": "positive",
            "entity_tags": ["X"],
        },
        {
            "id": "f2",
            "fact_type": "opinion",
            "sentiment": "concerning",
            "entity_tags": ["Y"],
        },
    ]
    signals = compute_signals(cluster={"title": "T", "member_facts": facts})
    assert signals["tension_count"] == 0
