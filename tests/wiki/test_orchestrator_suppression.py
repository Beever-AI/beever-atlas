"""Tests for the post-validation suppression pass.

The validator drops modules whose data SHAPE fails an eligibility
predicate. The suppression pass goes one step further: after the
plan validates, drop modules whose RENDERED output would be empty
or noise-only.

Covered rules:
1. ``entity_diagram`` noise — one dominant pair (>5 edges) AND
   ≤1 distinct relation verb across the graph.
2. ``entity_diagram`` thin — fewer than 2 distinct edge verbs
   (the same verb everywhere is relation-extraction noise).
3. ``flow_chart`` no-edges — process steps have zero ``to`` fields.
4. ``subpage_cards`` singleton — exactly one child.
5. ``mermaid_block_zero_edges`` (post-render) — any module whose
   rendered output is a Mermaid block with zero ``-->`` edges.

All five rules also assert telemetry log emission and order
preservation. The pre-render rules (1-4) live in
``_suppress_thin_modules``; rule 5 lives in
``_suppress_empty_mermaid_modules``.
"""

from __future__ import annotations

import logging
from typing import Iterator

import pytest

from beever_atlas.wiki.modules.orchestrator import (
    _suppress_empty_mermaid_modules,
    _suppress_thin_modules,
)
from beever_atlas.wiki.modules.planner import ModulePin, ModulePlan


# ---------------------------------------------------------------------------
# Telemetry harness — caplog can't see logs the project's structured
# JSON handler emits, so we attach a list-collecting handler directly
# to the orchestrator's logger and inspect the captured records.
# ---------------------------------------------------------------------------


@pytest.fixture
def captured_logs() -> Iterator[list[logging.LogRecord]]:
    """Yields a list that accumulates LogRecords from the orchestrator
    module. Cleans up its handler after the test."""

    records: list[logging.LogRecord] = []

    class _ListHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
            records.append(record)

    handler = _ListHandler(level=logging.INFO)
    logger = logging.getLogger("beever_atlas.wiki.modules.orchestrator")
    prior_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(prior_level)


def _suppressed_messages(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records if "module_suppressed" in r.getMessage()]


# ---------------------------------------------------------------------------
# Rule 1 — entity_diagram dominant pair + low verb diversity
# ---------------------------------------------------------------------------


def test_entity_diagram_dropped_when_dominated_by_one_pair_with_one_verb(
    captured_logs,
) -> None:
    plan = ModulePlan(modules=[{"id": "entity_diagram", "anchor": "ed"}])
    signals = {
        # 12 edges between Jack_Ng → Jacky_Chan, but only "REFERS" verb.
        "max_edges_between_same_pair": 12,
        "distinct_edge_verbs": 1,
        "process_step_edge_count": 0,
        "child_count": 0,
    }
    out = _suppress_thin_modules(plan, signals, {}, page_id="topic-42")
    assert out.modules == []
    # Telemetry — one structured log line, with the page id.
    messages = _suppressed_messages(captured_logs)
    assert len(messages) == 1
    assert "reason=entity_diagram_dominant_pair_one_verb" in messages[0]
    assert "module=entity_diagram" in messages[0]
    assert "page_id=topic-42" in messages[0]


def test_entity_diagram_kept_when_distinct_verbs_present() -> None:
    plan = ModulePlan(modules=[{"id": "entity_diagram", "anchor": "ed"}])
    signals = {
        "max_edges_between_same_pair": 12,
        "distinct_edge_verbs": 4,  # 4 distinct verbs — diverse graph
        "process_step_edge_count": 0,
        "child_count": 0,
    }
    out = _suppress_thin_modules(plan, signals, {})
    assert [m["id"] for m in out.modules] == ["entity_diagram"]


# ---------------------------------------------------------------------------
# Rule 2 — entity_diagram with <2 distinct verbs overall
# ---------------------------------------------------------------------------


def test_entity_diagram_dropped_when_only_one_distinct_verb_overall(
    captured_logs,
) -> None:
    plan = ModulePlan(modules=[{"id": "entity_diagram", "anchor": "ed"}])
    signals = {
        # 4 different pairs, but every edge has the same verb.
        "max_edges_between_same_pair": 1,
        "distinct_edge_verbs": 1,
        "process_step_edge_count": 0,
        "child_count": 0,
    }
    out = _suppress_thin_modules(plan, signals, {}, page_id="topic-7")
    assert out.modules == []
    assert any(
        "reason=entity_diagram_low_verb_diversity" in m for m in _suppressed_messages(captured_logs)
    )


def test_entity_diagram_kept_when_two_distinct_verbs() -> None:
    plan = ModulePlan(modules=[{"id": "entity_diagram", "anchor": "ed"}])
    signals = {
        "max_edges_between_same_pair": 1,
        "distinct_edge_verbs": 2,
        "process_step_edge_count": 0,
        "child_count": 0,
    }
    out = _suppress_thin_modules(plan, signals, {})
    assert [m["id"] for m in out.modules] == ["entity_diagram"]


# ---------------------------------------------------------------------------
# Rule 3 — flow_chart with no directed edges
# ---------------------------------------------------------------------------


def test_flow_chart_dropped_when_no_directed_edges(captured_logs) -> None:
    plan = ModulePlan(modules=[{"id": "flow_chart", "anchor": "fc"}])
    signals = {
        "max_edges_between_same_pair": 0,
        "distinct_edge_verbs": 0,
        "process_step_edge_count": 0,
        "child_count": 0,
    }
    out = _suppress_thin_modules(plan, signals, {}, page_id="topic-9")
    assert out.modules == []
    assert any(
        "reason=flow_chart_no_directed_edges" in m for m in _suppressed_messages(captured_logs)
    )


def test_flow_chart_kept_when_directed_edges_present() -> None:
    plan = ModulePlan(modules=[{"id": "flow_chart", "anchor": "fc"}])
    signals = {
        "max_edges_between_same_pair": 0,
        "distinct_edge_verbs": 0,
        "process_step_edge_count": 3,
        "child_count": 0,
    }
    out = _suppress_thin_modules(plan, signals, {})
    assert [m["id"] for m in out.modules] == ["flow_chart"]


# ---------------------------------------------------------------------------
# Rule 4 — subpage_cards with exactly one child
# ---------------------------------------------------------------------------


def test_subpage_cards_dropped_when_single_child(captured_logs) -> None:
    plan = ModulePlan(modules=[{"id": "subpage_cards", "anchor": "sc"}])
    signals = {
        "max_edges_between_same_pair": 0,
        "distinct_edge_verbs": 0,
        "process_step_edge_count": 0,
        "child_count": 1,
    }
    out = _suppress_thin_modules(plan, signals, {}, page_id="parent-1")
    assert out.modules == []
    assert any("reason=subpage_cards_singleton" in m for m in _suppressed_messages(captured_logs))


def test_subpage_cards_kept_when_two_or_more_children() -> None:
    plan = ModulePlan(modules=[{"id": "subpage_cards", "anchor": "sc"}])
    signals = {
        "max_edges_between_same_pair": 0,
        "distinct_edge_verbs": 0,
        "process_step_edge_count": 0,
        "child_count": 3,
    }
    out = _suppress_thin_modules(plan, signals, {})
    assert [m["id"] for m in out.modules] == ["subpage_cards"]


# ---------------------------------------------------------------------------
# Rule 5 — post-render Mermaid block with zero edges
# ---------------------------------------------------------------------------


def test_mermaid_block_post_render_dropped_when_zero_edges(captured_logs) -> None:
    plan = ModulePlan(modules=[{"id": "entity_diagram", "anchor": "ed"}])
    rendered = {
        "entity_diagram": ("```mermaid\ngraph TD\n    A[Foo]\n    B[Bar]\n    C[Baz]\n```"),
    }
    out_plan, out_rendered = _suppress_empty_mermaid_modules(
        plan, rendered, page_id="topic-mermaid"
    )
    assert out_plan.modules == []
    assert "entity_diagram" not in out_rendered
    messages = _suppressed_messages(captured_logs)
    assert any(
        "reason=mermaid_block_zero_edges" in m
        and "module=entity_diagram" in m
        and "page_id=topic-mermaid" in m
        for m in messages
    )


def test_mermaid_block_post_render_kept_when_edges_present() -> None:
    plan = ModulePlan(modules=[{"id": "entity_diagram", "anchor": "ed"}])
    rendered = {
        "entity_diagram": ("```mermaid\ngraph TD\n    A[Foo]\n    B[Bar]\n    A --> B\n```"),
    }
    out_plan, out_rendered = _suppress_empty_mermaid_modules(plan, rendered)
    assert [m["id"] for m in out_plan.modules] == ["entity_diagram"]
    assert "entity_diagram" in out_rendered


def test_mermaid_block_post_render_keeps_labelled_arrows() -> None:
    """Labelled mermaid arrows ``A -->|label| B`` still contain ``-->``
    so the suppressor must keep them."""
    plan = ModulePlan(modules=[{"id": "entity_diagram", "anchor": "ed"}])
    rendered = {
        "entity_diagram": (
            "```mermaid\ngraph TD\n    A[Foo]\n    B[Bar]\n    A -->|relates_to| B\n```"
        ),
    }
    out_plan, _ = _suppress_empty_mermaid_modules(plan, rendered)
    assert [m["id"] for m in out_plan.modules] == ["entity_diagram"]


# ---------------------------------------------------------------------------
# Cross-cutting properties — order preservation, immutability,
# multiple modules in a plan.
# ---------------------------------------------------------------------------


def test_suppress_thin_modules_preserves_order_for_kept_modules() -> None:
    plan = ModulePlan(
        modules=[
            {"id": "key_facts", "anchor": "kf"},
            {"id": "subpage_cards", "anchor": "sc"},  # will be dropped (singleton)
            {"id": "decision_log", "anchor": "dl"},
            {"id": "open_questions", "anchor": "oq"},
        ]
    )
    signals = {
        "max_edges_between_same_pair": 0,
        "distinct_edge_verbs": 0,
        "process_step_edge_count": 0,
        "child_count": 1,
    }
    out = _suppress_thin_modules(plan, signals, {})
    assert [m["id"] for m in out.modules] == [
        "key_facts",
        "decision_log",
        "open_questions",
    ]


def test_suppress_thin_modules_returns_new_plan_does_not_mutate_input() -> None:
    original_modules = [
        {"id": "subpage_cards", "anchor": "sc"},
        {"id": "key_facts", "anchor": "kf"},
    ]
    pins = [ModulePin(media_id="m1", fact_id="f1", slot="inline")]
    plan = ModulePlan(modules=list(original_modules), media_pins=list(pins))
    signals = {
        "max_edges_between_same_pair": 0,
        "distinct_edge_verbs": 0,
        "process_step_edge_count": 0,
        "child_count": 1,
    }
    out = _suppress_thin_modules(plan, signals, {})
    # Returned plan is a new object — callers should treat input as read-only.
    assert out is not plan
    # Input list still has both modules.
    assert plan.modules == original_modules
    # Output dropped subpage_cards.
    assert [m["id"] for m in out.modules] == ["key_facts"]
    # Media pins propagate.
    assert len(out.media_pins) == 1
    assert out.media_pins[0].media_id == "m1"


def test_suppress_thin_modules_unknown_module_passes_through() -> None:
    """The suppression pass only knows about a small set of module
    IDs. Modules it doesn't recognise pass through untouched — the
    validator already dropped any catalog-unknown entries."""
    plan = ModulePlan(modules=[{"id": "key_facts", "anchor": "kf"}])
    signals = {
        "max_edges_between_same_pair": 999,
        "distinct_edge_verbs": 1,
        "process_step_edge_count": 0,
        "child_count": 1,
    }
    out = _suppress_thin_modules(plan, signals, {})
    assert [m["id"] for m in out.modules] == ["key_facts"]


def test_suppress_empty_mermaid_returns_new_plan() -> None:
    plan = ModulePlan(
        modules=[
            {"id": "entity_diagram", "anchor": "ed"},
            {"id": "key_facts", "anchor": "kf"},
        ]
    )
    rendered = {
        "entity_diagram": "```mermaid\ngraph TD\n    A[Foo]\n```",  # zero edges
        "key_facts": "| F | V |\n|---|---|\n| a | b |",
    }
    out_plan, out_rendered = _suppress_empty_mermaid_modules(plan, rendered)
    # Input is not mutated.
    assert out_plan is not plan
    assert [m["id"] for m in plan.modules] == ["entity_diagram", "key_facts"]
    assert [m["id"] for m in out_plan.modules] == ["key_facts"]
    # Rendered dict for the dropped module is removed; others retained.
    assert "entity_diagram" not in out_rendered
    assert "key_facts" in out_rendered


def test_suppress_empty_mermaid_skips_non_mermaid_modules() -> None:
    """Modules whose rendered output is not a Mermaid block must not
    be touched — the rule only applies to modules whose primary
    output is Mermaid."""
    plan = ModulePlan(modules=[{"id": "decision_log", "anchor": "dl"}])
    rendered = {"decision_log": "| Decision | Status |\n|---|---|\n| Adopt | active |"}
    out_plan, out_rendered = _suppress_empty_mermaid_modules(plan, rendered)
    assert [m["id"] for m in out_plan.modules] == ["decision_log"]
    assert "decision_log" in out_rendered
