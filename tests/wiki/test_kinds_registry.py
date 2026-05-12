"""Tests for the adaptive page-kind registry + predicates."""

from __future__ import annotations

from beever_atlas.wiki.kinds import (
    KIND_REGISTRY,
    ChannelSignals,
    adaptive_kinds,
    architecture_predicate,
    open_questions_predicate,
    projects_predicate,
    should_instantiate,
    stakeholders_predicate,
    timeline_predicate,
)


# ---------------------------------------------------------------------------
# projects predicate
# ---------------------------------------------------------------------------


def test_projects_predicate_above_threshold_via_facts() -> None:
    s = ChannelSignals(fact_count_by_type={"project": 5})
    should, _reason = projects_predicate(s)
    assert should is True


def test_projects_predicate_above_threshold_via_clusters() -> None:
    s = ChannelSignals(project_cluster_count=2)
    should, _reason = projects_predicate(s)
    assert should is True


def test_projects_predicate_below_threshold() -> None:
    s = ChannelSignals(fact_count_by_type={"project": 1}, project_cluster_count=1)
    should, _reason = projects_predicate(s)
    assert should is False


# ---------------------------------------------------------------------------
# architecture predicate
# ---------------------------------------------------------------------------


def test_architecture_predicate_above_threshold() -> None:
    s = ChannelSignals(entity_count_by_type={"system": 6, "service": 5})
    should, _reason = architecture_predicate(s)
    assert should is True  # 6+5=11 ≥10


def test_architecture_predicate_below_threshold() -> None:
    s = ChannelSignals(entity_count_by_type={"system": 4, "service": 3})
    should, _reason = architecture_predicate(s)
    assert should is False


# ---------------------------------------------------------------------------
# open_questions predicate
# ---------------------------------------------------------------------------


def test_open_questions_predicate_above_threshold() -> None:
    s = ChannelSignals(
        fact_count_by_type={"open_question": 5},
        open_question_resolved_ratio=0.4,
    )
    should, _reason = open_questions_predicate(s)
    assert should is True


def test_open_questions_predicate_all_resolved_suppresses() -> None:
    s = ChannelSignals(
        fact_count_by_type={"open_question": 8},
        open_question_resolved_ratio=1.0,
    )
    should, _reason = open_questions_predicate(s)
    assert should is False


def test_open_questions_predicate_below_count_suppresses() -> None:
    s = ChannelSignals(
        fact_count_by_type={"open_question": 2},
        open_question_resolved_ratio=0.0,
    )
    should, _reason = open_questions_predicate(s)
    assert should is False


# ---------------------------------------------------------------------------
# timeline predicate
# ---------------------------------------------------------------------------


def test_timeline_predicate_above_threshold() -> None:
    s = ChannelSignals(channel_age_days=60, activity_epoch_count=4)
    should, _reason = timeline_predicate(s)
    assert should is True


def test_timeline_predicate_too_young() -> None:
    s = ChannelSignals(channel_age_days=14, activity_epoch_count=4)
    should, _reason = timeline_predicate(s)
    assert should is False


def test_timeline_predicate_too_few_epochs() -> None:
    s = ChannelSignals(channel_age_days=120, activity_epoch_count=2)
    should, _reason = timeline_predicate(s)
    assert should is False


# ---------------------------------------------------------------------------
# stakeholders predicate
# ---------------------------------------------------------------------------


def test_stakeholders_predicate_above_threshold() -> None:
    s = ChannelSignals(distinct_contributor_count=15, role_hierarchy_detected=True)
    should, _reason = stakeholders_predicate(s)
    assert should is True


def test_stakeholders_predicate_no_hierarchy_suppresses() -> None:
    s = ChannelSignals(distinct_contributor_count=20, role_hierarchy_detected=False)
    should, _reason = stakeholders_predicate(s)
    assert should is False


def test_stakeholders_predicate_too_few_contributors() -> None:
    s = ChannelSignals(distinct_contributor_count=5, role_hierarchy_detected=True)
    should, _reason = stakeholders_predicate(s)
    assert should is False


# ---------------------------------------------------------------------------
# Registry + override behavior
# ---------------------------------------------------------------------------


def test_required_kinds_always_instantiate() -> None:
    s = ChannelSignals()
    for kind in ("overview", "topic", "people", "glossary", "decisions", "faq"):
        should, reason = should_instantiate(kind, s)
        assert should is True, kind
        assert reason == "required"


def test_force_kinds_overrides_predicate() -> None:
    """Operator's force_kinds beats a False predicate."""
    s = ChannelSignals(force_kinds=frozenset({"projects"}))
    should, reason = should_instantiate("projects", s)
    assert should is True
    assert reason == "policy:force"


def test_suppress_kinds_overrides_required() -> None:
    """Operator's suppress_kinds beats even required gating."""
    s = ChannelSignals(suppress_kinds=frozenset({"projects"}))
    should, reason = should_instantiate("projects", s)
    assert should is False
    assert reason == "policy:suppress"


def test_suppress_kinds_overrides_predicate_true() -> None:
    s = ChannelSignals(
        fact_count_by_type={"project": 10},
        suppress_kinds=frozenset({"projects"}),
    )
    should, reason = should_instantiate("projects", s)
    assert should is False
    assert reason == "policy:suppress"


def test_unknown_kind_returns_false() -> None:
    s = ChannelSignals()
    should, reason = should_instantiate("rainbow-page", s)
    assert should is False
    assert reason == "unknown_kind"


def test_adaptive_kinds_listing() -> None:
    expected = {
        "projects",
        "architecture",
        "open-questions",
        "timeline",
        "stakeholders",
    }
    assert set(adaptive_kinds()) == expected


def test_registry_has_all_kinds() -> None:
    """Registry exposes every kind from the design."""
    must_exist = {
        "overview",
        "topic",
        "people",
        "glossary",
        "decisions",
        "faq",
        "activity",
        "resources",
        "projects",
        "architecture",
        "open-questions",
        "timeline",
        "stakeholders",
    }
    assert must_exist.issubset(KIND_REGISTRY.keys())
