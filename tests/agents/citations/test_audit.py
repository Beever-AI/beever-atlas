"""Unit tests for Phase 3 audit helpers and tool coverage verification."""

from __future__ import annotations

from beever_atlas.agents.citations.audit import (
    EXEMPT_TOOLS,
    dedup_by_id,
    iter_sources_from_message,
    verify_tool_coverage,
)


# ---- verify_tool_coverage --------------------------------------------


def test_tool_coverage_is_complete_on_main():
    """Every citable retrieval tool must be wrapped by @cite_tool_output."""
    report = verify_tool_coverage()
    assert report["uncovered"] == [], (
        "Found un-decorated retrieval tools; wrap them with "
        "@cite_tool_output(kind=...) or add to EXEMPT_TOOLS with justification: "
        f"{report['uncovered']}"
    )
    # Positive sanity: the memory tools we decorated should appear covered.
    covered_joined = " ".join(report["covered"])
    assert "memory_tools.search_channel_facts" in covered_joined
    assert "memory_tools.search_qa_history" in covered_joined
    assert "wiki_tools.get_wiki_page" in covered_joined
    assert "external_tools.search_external_knowledge" in covered_joined
    assert "graph_tools.trace_decision_history" in covered_joined


def test_coverage_reports_at_least_one_exempt():
    """EXEMPT_TOOLS must not be empty — we use `resolve_channel_name` at minimum."""
    report = verify_tool_coverage()
    assert len(report["exempt"]) >= 1


def test_exempt_list_includes_channel_resolver():
    assert "resolve_channel_name" in EXEMPT_TOOLS


# ---- iter_sources_from_message + dedup_by_id -------------------------


def test_iter_sources_from_envelope_message():
    msg = {
        "citations": {
            "items": [],
            "sources": [
                {"id": "src_1", "kind": "channel_message"},
                {"id": "src_2", "kind": "web_result"},
            ],
            "refs": [],
        }
    }
    sources = list(iter_sources_from_message(msg))
    assert len(sources) == 2
    assert [s["id"] for s in sources] == ["src_1", "src_2"]


def test_iter_sources_from_legacy_message_returns_empty():
    msg = {"citations": [{"author": "alice", "channel": "x"}]}
    sources = list(iter_sources_from_message(msg))
    assert sources == []  # Legacy list has no structured sources.


def test_dedup_by_id_preserves_first_seen_order():
    sources = [
        {"id": "a"},
        {"id": "b"},
        {"id": "a"},  # dup
        {"id": "c"},
    ]
    out = dedup_by_id(sources)
    assert [s["id"] for s in out] == ["a", "b", "c"]


def test_dedup_skips_entries_without_id():
    sources = [{"id": "a"}, {}, {"id": "b"}]
    out = dedup_by_id(sources)
    assert [s["id"] for s in out] == ["a", "b"]
