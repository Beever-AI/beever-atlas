"""Adaptive page-kind registry + predicate gating.

wiki-redesign-gap-fill / Group 7+8 — centralises page-kind metadata so the
Builder, Maintainer, and structure planner consult one source of truth for
which kinds are eligible to be instantiated for a given channel.

Design D3:

* **Required kinds** (always instantiated): ``overview``, ``topic``,
  ``people``, ``glossary``, ``decisions``, ``faq``, ``activity``,
  ``resources``.
* **Adaptive kinds** (predicate-gated): ``projects``, ``architecture``,
  ``open-questions``, ``timeline``, ``stakeholders``.

Operator overrides via channel policy:

* ``wiki.force_kinds: list[str]`` — instantiate even if predicate returns
  False.
* ``wiki.suppress_kinds: list[str]`` — never instantiate even if predicate
  returns True.

Predicate inputs are aggregated into ``ChannelSignals`` so each predicate
function is a pure check against a frozen snapshot of the channel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable


# ----------------------------------------------------------------------
# Channel signal snapshot
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class ChannelSignals:
    """Frozen snapshot of corpus signals predicates evaluate against.

    Built by the Builder / structure planner from the gathered data
    BEFORE the per-page compile loop runs. Predicates are pure functions
    of this snapshot so they're cheap, deterministic, and unit-testable.
    """

    fact_count_by_type: dict[str, int] = field(default_factory=dict)
    """``{fact_type: count}`` — drives `projects`, `open-questions` predicates."""

    project_cluster_count: int = 0
    """Count of clusters with archetype=project — drives `projects` predicate."""

    entity_count_by_type: dict[str, int] = field(default_factory=dict)
    """``{entity_type: count of distinct entities}`` — drives `architecture`."""

    channel_age_days: int = 0
    """Days between earliest and latest message — drives `timeline`."""

    activity_epoch_count: int = 0
    """Count of distinct activity epochs (≥7-day gaps) — drives `timeline`."""

    distinct_contributor_count: int = 0
    """Count of distinct people who contributed — drives `stakeholders`."""

    role_hierarchy_detected: bool = False
    """Whether the entity graph reveals a role hierarchy — drives `stakeholders`."""

    open_question_resolved_ratio: float = 1.0
    """Ratio of resolved/total open questions — drives `open-questions`."""

    force_kinds: frozenset[str] = field(default_factory=frozenset)
    """Channel-policy override: instantiate even when predicate is False."""

    suppress_kinds: frozenset[str] = field(default_factory=frozenset)
    """Channel-policy override: never instantiate even when predicate is True."""


# ----------------------------------------------------------------------
# Predicate functions
# ----------------------------------------------------------------------


def projects_predicate(s: ChannelSignals) -> tuple[bool, str]:
    """≥3 project-typed facts OR ≥2 project-archetype clusters."""
    p_facts = s.fact_count_by_type.get("project", 0)
    if p_facts >= 3:
        return True, f"{p_facts} project facts ≥3"
    if s.project_cluster_count >= 2:
        return True, f"{s.project_cluster_count} project clusters ≥2"
    return False, (f"only {p_facts} project facts and {s.project_cluster_count} project clusters")


def architecture_predicate(s: ChannelSignals) -> tuple[bool, str]:
    """≥10 distinct entities of type system or service."""
    systems = s.entity_count_by_type.get("system", 0)
    services = s.entity_count_by_type.get("service", 0)
    total = systems + services
    if total >= 10:
        return True, f"{total} distinct system/service entities ≥10"
    return False, f"only {total} distinct system/service entities"


def open_questions_predicate(s: ChannelSignals) -> tuple[bool, str]:
    """≥3 open-question facts AND not all resolved."""
    oq_facts = s.fact_count_by_type.get("open_question", 0)
    if oq_facts < 3:
        return False, f"only {oq_facts} open_question facts"
    if s.open_question_resolved_ratio >= 1.0:
        return False, "all open questions resolved — page would be empty"
    return True, (
        f"{oq_facts} open_question facts with {s.open_question_resolved_ratio:.0%} resolved"
    )


def timeline_predicate(s: ChannelSignals) -> tuple[bool, str]:
    """Channel age ≥30 days AND ≥3 activity epochs."""
    if s.channel_age_days < 30:
        return False, f"channel age {s.channel_age_days}d <30"
    if s.activity_epoch_count < 3:
        return False, f"only {s.activity_epoch_count} activity epochs"
    return True, (f"channel age {s.channel_age_days}d ≥30 with {s.activity_epoch_count} epochs ≥3")


def stakeholders_predicate(s: ChannelSignals) -> tuple[bool, str]:
    """≥10 distinct contributors AND role hierarchy detectable."""
    if s.distinct_contributor_count < 10:
        return False, f"only {s.distinct_contributor_count} distinct contributors"
    if not s.role_hierarchy_detected:
        return False, "no role hierarchy detected in entity graph"
    return True, (
        f"{s.distinct_contributor_count} distinct contributors with detectable role hierarchy"
    )


# ----------------------------------------------------------------------
# Registry
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class KindSpec:
    """One entry in :data:`KIND_REGISTRY`."""

    kind: str
    is_required: bool
    """Required kinds always instantiate; adaptive kinds gate on ``predicate``."""
    predicate: Callable[[ChannelSignals], tuple[bool, str]] | None = None
    """Returns ``(should_instantiate, reason)``. ``None`` for required kinds."""
    prompt_path: str | None = None
    """Filesystem path to the kind's prompt file, relative to ``wiki/prompts/``."""


KIND_REGISTRY: dict[str, KindSpec] = {
    # Required kinds — always instantiated regardless of signals.
    "overview": KindSpec(kind="overview", is_required=True),
    "topic": KindSpec(kind="topic", is_required=True),
    "people": KindSpec(kind="people", is_required=True),
    "glossary": KindSpec(kind="glossary", is_required=True),
    "decisions": KindSpec(kind="decisions", is_required=True),
    "faq": KindSpec(kind="faq", is_required=True),
    "activity": KindSpec(kind="activity", is_required=True),
    "resources": KindSpec(kind="resources", is_required=True),
    # Adaptive kinds — predicate-gated.
    "projects": KindSpec(
        kind="projects",
        is_required=False,
        predicate=projects_predicate,
        prompt_path="projects.txt",
    ),
    "architecture": KindSpec(
        kind="architecture",
        is_required=False,
        predicate=architecture_predicate,
        prompt_path="architecture.txt",
    ),
    "open-questions": KindSpec(
        kind="open-questions",
        is_required=False,
        predicate=open_questions_predicate,
        prompt_path="open-questions.txt",
    ),
    "timeline": KindSpec(
        kind="timeline",
        is_required=False,
        predicate=timeline_predicate,
        prompt_path="timeline.txt",
    ),
    "stakeholders": KindSpec(
        kind="stakeholders",
        is_required=False,
        predicate=stakeholders_predicate,
        prompt_path="stakeholders.txt",
    ),
}


# ----------------------------------------------------------------------
# Adaptive instantiation gate
# ----------------------------------------------------------------------


def should_instantiate(kind: str, signals: ChannelSignals) -> tuple[bool, str]:
    """Decide whether to instantiate a page of ``kind`` for the channel.

    Order of precedence (per design D3):

    1. ``suppress_kinds`` policy override → False, "policy:suppress".
    2. Required kinds → True, "required".
    3. ``force_kinds`` policy override → True, "policy:force".
    4. Predicate result for adaptive kinds.
    5. Unknown kinds → False, "unknown_kind".
    """
    if kind in signals.suppress_kinds:
        return False, "policy:suppress"
    spec = KIND_REGISTRY.get(kind)
    if spec is None:
        return False, "unknown_kind"
    if spec.is_required:
        return True, "required"
    if kind in signals.force_kinds:
        return True, "policy:force"
    if spec.predicate is None:
        return False, "no_predicate"
    return spec.predicate(signals)


def adaptive_kinds() -> list[str]:
    """Return the canonical list of adaptive (predicate-gated) kind names."""
    return [k for k, spec in KIND_REGISTRY.items() if not spec.is_required]
