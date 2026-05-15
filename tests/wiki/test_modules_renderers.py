"""Unit tests for the 11 deterministic content module renderers.

Each test exercises:
- Happy path: realistic data → expected markdown shape
- Empty path: missing or empty input → empty string (caller decides
  whether to skip the module entirely)
- Robustness: malformed input → empty string or graceful degrade,
  never raise

Module renderers are pure functions — these tests run without any
LLM, network, or DB.
"""

from __future__ import annotations

from beever_atlas.wiki.modules import (
    comparison_matrix,
    decision_log,
    entity_diagram,
    flow_chart,
    key_facts,
    open_questions,
    pros_cons,
    quote_highlights,
    related_threads,
    subpage_cards,
    timeline,
)


# ---------------------------------------------------------------------------
# key_facts
# ---------------------------------------------------------------------------


def test_key_facts_renders_table_for_real_data() -> None:
    data = {
        "facts": [
            {"memory_text": "X happened", "fact_type": "event", "importance": 5},
            {"memory_text": "Y was decided", "fact_type": "decision", "importance": 8},
        ]
    }
    out = key_facts.render(data)
    assert "| Fact" in out
    assert "X happened" in out
    assert "Y was decided" in out


def test_key_facts_empty_returns_empty_string() -> None:
    assert key_facts.render({}) == ""
    assert key_facts.render({"facts": []}) == ""


# ---------------------------------------------------------------------------
# decision_log
# ---------------------------------------------------------------------------


def test_decision_log_renders_table_with_status_badges() -> None:
    data = {
        "decisions": [
            {"decision": "Adopt JWT", "status": "active", "made_by": "Jacky", "date": "2026-04-15"},
            {
                "decision": "Use Mattermost",
                "status": "superseded",
                "made_by": "Thomas",
                "date": "2026-04-10",
            },
        ]
    }
    out = decision_log.render(data)
    assert "| Decision |" in out
    assert "Adopt JWT" in out
    assert "✅ active" in out
    assert "❌ superseded" in out


def test_decision_log_unknown_status_falls_through() -> None:
    out = decision_log.render({"decisions": [{"decision": "X", "status": "weird-status"}]})
    assert "weird-status" in out  # raw status string preserved


def test_decision_log_empty_returns_empty() -> None:
    assert decision_log.render({}) == ""
    assert decision_log.render({"decisions": []}) == ""


# ---------------------------------------------------------------------------
# timeline
# ---------------------------------------------------------------------------


def test_timeline_orders_events_ascending_by_date() -> None:
    data = {
        "events": [
            {"date": "2026-04-20", "event": "Second event"},
            {"date": "2026-04-10", "event": "First event"},
            {"date": "2026-04-15", "event": "Middle event"},
        ]
    }
    out = timeline.render(data)
    lines = out.splitlines()
    assert "First event" in lines[0]
    assert "Middle event" in lines[1]
    assert "Second event" in lines[2]


def test_timeline_undated_events_sort_last() -> None:
    data = {
        "events": [
            {"event": "Undated"},
            {"date": "2026-04-10", "event": "Dated"},
        ]
    }
    out = timeline.render(data)
    lines = out.splitlines()
    assert "Dated" in lines[0]
    assert "Undated" in lines[1]


def test_timeline_empty_returns_empty() -> None:
    assert timeline.render({}) == ""


# ---------------------------------------------------------------------------
# comparison_matrix
# ---------------------------------------------------------------------------


def test_comparison_matrix_renders_with_alts_and_criteria() -> None:
    data = {
        "alternatives": ["Option A", "Option B"],
        "criteria": [
            {"name": "Cost", "values": {"Option A": "$10/mo", "Option B": "$25/mo"}},
            {"name": "Latency", "values": {"Option A": "200ms", "Option B": "50ms"}},
        ],
    }
    out = comparison_matrix.render(data)
    assert "| Criterion | Option A | Option B |" in out
    assert "Cost" in out
    assert "$10/mo" in out
    assert "50ms" in out


def test_comparison_matrix_requires_two_alternatives() -> None:
    out = comparison_matrix.render(
        {"alternatives": ["Solo"], "criteria": [{"name": "X", "values": {"Solo": "y"}}]}
    )
    assert out == ""


def test_comparison_matrix_pads_missing_cells() -> None:
    data = {
        "alternatives": ["A", "B"],
        "criteria": [{"name": "Cost", "values": {"A": "$10"}}],  # B missing
    }
    out = comparison_matrix.render(data)
    # Row should still emit with B as a single space.
    assert "| Cost | $10 |   |" in out or "| Cost | $10 | " in out


# ---------------------------------------------------------------------------
# pros_cons
# ---------------------------------------------------------------------------


def test_pros_cons_renders_two_column_table() -> None:
    data = {
        "pros": ["Faster", {"text": "Cheaper", "citations": "[1]"}],
        "cons": ["Less mature"],
    }
    out = pros_cons.render(data)
    assert "| Pros | Cons |" in out
    assert "Faster" in out
    assert "Cheaper [1]" in out
    assert "Less mature" in out


def test_pros_cons_pads_uneven_lists() -> None:
    out = pros_cons.render({"pros": ["A", "B", "C"], "cons": ["X"]})
    # 3 pros, 1 con → 3 rows, the latter 2 cons are blank.
    assert out.count("\n") >= 4


def test_pros_cons_both_empty_returns_empty() -> None:
    assert pros_cons.render({"pros": [], "cons": []}) == ""


# ---------------------------------------------------------------------------
# quote_highlights
# ---------------------------------------------------------------------------


def test_quote_highlights_renders_blockquotes() -> None:
    data = {
        "quotes": [
            {
                "text": "We must ship by Friday",
                "author": "Thomas",
                "date": "2026-04-15",
                "citations": "[1]",
            },
            {
                "text": "I disagree, the test plan isn't ready",
                "author": "Jacky",
                "date": "2026-04-15",
            },
        ]
    }
    out = quote_highlights.render(data)
    assert '> "We must ship by Friday" — Thomas, 2026-04-15 [1]' in out
    assert '> "I disagree, the test plan isn\'t ready" — Jacky, 2026-04-15' in out


def test_quote_highlights_flattens_multiline_text() -> None:
    out = quote_highlights.render({"quotes": [{"text": "line one\nline two"}]})
    assert "\n" not in out.splitlines()[0]  # the blockquote line itself
    assert "line one line two" in out


def test_quote_highlights_empty_returns_empty() -> None:
    assert quote_highlights.render({}) == ""


# ---------------------------------------------------------------------------
# flow_chart
# ---------------------------------------------------------------------------


def test_flow_chart_renders_mermaid_block() -> None:
    data = {
        "steps": [
            {"id": "ingest", "label": "Ingest"},
            {"id": "extract", "label": "Extract facts"},
            {"id": "compile", "label": "Compile pages"},
        ],
        "edges": [
            {"from": "ingest", "to": "extract", "label": "messages"},
            {"from": "extract", "to": "compile"},
        ],
    }
    out = flow_chart.render(data)
    assert out.startswith("```mermaid\ngraph LR")
    assert out.endswith("```")
    assert "ingest[Ingest]" in out
    assert "ingest -->|messages| extract" in out
    assert "extract --> compile" in out


def test_flow_chart_drops_edges_to_unknown_nodes() -> None:
    data = {
        "steps": [{"id": "a", "label": "A"}],
        "edges": [{"from": "a", "to": "ghost"}],  # ghost not in steps
    }
    out = flow_chart.render(data)
    assert "ghost" not in out


def test_flow_chart_empty_returns_empty() -> None:
    assert flow_chart.render({}) == ""


# ---------------------------------------------------------------------------
# entity_diagram
# ---------------------------------------------------------------------------


def test_entity_diagram_requires_thresholds() -> None:
    # 3 entities + 5 edges = the documented threshold.
    data = {
        "entities": [
            {"id": "A", "label": "A"},
            {"id": "B", "label": "B"},
            {"id": "C", "label": "C"},
        ],
        "relationships": [
            {"from": "A", "to": "B"},
            {"from": "B", "to": "C"},
            {"from": "C", "to": "A"},
            {"from": "A", "to": "C"},
            {"from": "B", "to": "A"},
        ],
    }
    out = entity_diagram.render(data)
    assert out.startswith("```mermaid\ngraph TD")


def test_entity_diagram_below_threshold_returns_empty() -> None:
    data = {
        "entities": [{"id": "A", "label": "A"}, {"id": "B", "label": "B"}],
        "relationships": [{"from": "A", "to": "B"}],
    }
    assert entity_diagram.render(data) == ""


def test_entity_diagram_uses_round_shape_for_people() -> None:
    data = {
        "entities": [
            {"id": "P1", "label": "Jacky", "kind": "person"},
            {"id": "P2", "label": "Thomas", "kind": "person"},
            {"id": "T1", "label": "Auth", "kind": "topic"},
        ],
        "relationships": [
            {"from": "P1", "to": "T1"},
            {"from": "P2", "to": "T1"},
            {"from": "P1", "to": "P2"},
            {"from": "P2", "to": "P1"},
            {"from": "T1", "to": "P1"},
        ],
    }
    out = entity_diagram.render(data)
    assert "P1(Jacky)" in out  # round shape
    assert "T1[Auth]" in out  # square shape


# ---------------------------------------------------------------------------
# open_questions
# ---------------------------------------------------------------------------


def test_open_questions_renders_with_dates() -> None:
    data = {
        "questions": [
            {"question": "Why does X conflict?", "raised": "2026-04-15", "citations": "[3]"},
            "Should we adopt Y?",  # plain string fallback
        ]
    }
    out = open_questions.render(data)
    assert "**(raised 2026-04-15)** Why does X conflict? [3]" in out
    assert "Should we adopt Y?" in out


def test_open_questions_empty_returns_empty() -> None:
    assert open_questions.render({}) == ""


# ---------------------------------------------------------------------------
# subpage_cards
# ---------------------------------------------------------------------------


def test_subpage_cards_delegates_to_children_toc() -> None:
    out = subpage_cards.render({"children": [{"title": "Auth", "slug": "topic-auth"}]})
    assert "[Auth](/wiki/topic-auth)" in out


def test_subpage_cards_empty_returns_empty() -> None:
    assert subpage_cards.render({}) == ""


# ---------------------------------------------------------------------------
# related_threads
# ---------------------------------------------------------------------------


def test_related_threads_caps_at_five() -> None:
    data = {
        "related": [
            {"title": f"Topic {i}", "slug": f"t{i}", "reason": f"shares X{i}"} for i in range(20)
        ]
    }
    out = related_threads.render(data)
    assert out.count("\n- ") + (1 if out.startswith("- ") else 0) == 5


def test_related_threads_renders_links_with_reasons() -> None:
    out = related_threads.render(
        {
            "related": [
                {"title": "Auth", "slug": "topic-auth", "reason": "shared entity Jacky"},
            ]
        }
    )
    assert "**[Auth](/wiki/topic-auth)** — shared entity Jacky" in out


def test_related_threads_empty_returns_empty() -> None:
    assert related_threads.render({}) == ""


# ---------------------------------------------------------------------------
# Robustness — every renderer is total over malformed input
# ---------------------------------------------------------------------------


def test_all_renderers_return_string_on_garbage_input() -> None:
    """Defensive: planners can emit unexpected shapes. Renderers must
    never raise — empty string is the safe fallback (the dispatcher
    decides whether to skip the module entirely)."""
    garbage_inputs = [
        {},
        {"facts": "not a list"},
        {"decisions": [42, "string", None]},
        {"events": None},
        {"alternatives": "single"},
        {"steps": [None, {"id": ""}]},
        {"entities": "x"},
        {"questions": [123]},
        {"children": "not a list"},
        {"related": [None, "string"]},
        {"quotes": [None]},
        {"pros": "string", "cons": [1, 2]},
    ]
    renderers = [
        key_facts.render,
        decision_log.render,
        timeline.render,
        comparison_matrix.render,
        pros_cons.render,
        quote_highlights.render,
        flow_chart.render,
        entity_diagram.render,
        open_questions.render,
        subpage_cards.render,
        related_threads.render,
    ]
    for r in renderers:
        for g in garbage_inputs:
            out = r(g)
            assert isinstance(out, str), f"{r.__module__} returned non-string for {g}"
