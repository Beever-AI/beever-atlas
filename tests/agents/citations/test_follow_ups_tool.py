"""Unit tests for the suggest_follow_ups ADK tool."""

from __future__ import annotations

from beever_atlas.agents.query.follow_ups_tool import (
    bind_collector,
    current_collector,
    reset_collector,
    suggest_follow_ups,
)


def test_no_collector_bound_is_safe():
    # Should not throw when called outside a turn context.
    result = suggest_follow_ups(["Q1?"])
    assert result == {"ok": True, "count": 1}


def test_collector_receives_cleaned_questions():
    c, tok = bind_collector()
    try:
        result = suggest_follow_ups([
            "  What next?  ",
            "",
            "When did this happen?",
        ])
    finally:
        reset_collector(tok)
    assert result == {"ok": True, "count": 2}
    assert c.questions == ["What next?", "When did this happen?"]


def test_caps_at_three_questions():
    c, tok = bind_collector()
    try:
        suggest_follow_ups(["Q1?", "Q2?", "Q3?", "Q4?", "Q5?"])
    finally:
        reset_collector(tok)
    assert c.questions == ["Q1?", "Q2?", "Q3?"]


def test_non_list_input_ignored():
    c, tok = bind_collector()
    try:
        suggest_follow_ups("not a list")  # type: ignore[arg-type]
    finally:
        reset_collector(tok)
    assert c.questions == []


def test_non_string_entries_dropped():
    c, tok = bind_collector()
    try:
        suggest_follow_ups(["ok?", 123, None, "also ok?"])  # type: ignore[list-item]
    finally:
        reset_collector(tok)
    assert c.questions == ["ok?", "also ok?"]


def test_scrubs_bogus_src_literal_from_question():
    """The LLM sometimes copies a tool-result citation literal into a
    follow-up question (`[src:get_wiki_page_response]`). The UI renders
    these as raw bracket text, so the tool must scrub them before the
    collector stores the question."""
    c, tok = bind_collector()
    try:
        suggest_follow_ups([
            "Why did that fail [src:get_wiki_page_response]?",
            "What happened [External: meeting] here?",
            "Plain question?",
        ])
    finally:
        reset_collector(tok)
    assert c.questions == [
        "Why did that fail ?",
        "What happened here?",
        "Plain question?",
    ]
    for q in c.questions:
        assert "[src:" not in q
        assert "[External:" not in q


def test_current_collector_isolation():
    assert current_collector() is None
    c, tok = bind_collector()
    try:
        assert current_collector() is c
    finally:
        reset_collector(tok)
    assert current_collector() is None
