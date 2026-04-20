"""Tests for marker-splice behavior in the topic compile path (Phase 4)."""

from __future__ import annotations

from beever_atlas.wiki.compiler import _splice_key_facts_table


_FACTS = [
    {
        "memory_text": "alpha",
        "author_name": "a",
        "fact_type": "claim",
        "importance": 0.9,
        "quality_score": 0.9,
    },
    {
        "memory_text": "beta",
        "author_name": "b",
        "fact_type": "claim",
        "importance": 0.8,
        "quality_score": 0.8,
    },
]


def test_compile_topic_injects_deterministic_table() -> None:
    content = "**TL;DR** thing.\n\n## Key Facts\n\n<<KEY_FACTS_TABLE>>\n\n## Overview\n\ntext"
    out = _splice_key_facts_table(content, _FACTS)
    assert "<<KEY_FACTS_TABLE>>" not in out
    assert "| Fact | Source | Type | Importance |" in out
    assert "alpha" in out
    assert "beta" in out


def test_compile_topic_injects_when_marker_missing() -> None:
    content = (
        "**TL;DR** thing.\n\n"
        "## TL;DR\n\nlead line\n\n"
        "## Overview\n\nsome body\n\n"
        "## Decisions\n\nmore\n"
    )
    out = _splice_key_facts_table(content, _FACTS)
    # Table inserted somewhere after first non-TL;DR heading (## Overview).
    overview_idx = out.find("## Overview")
    table_idx = out.find("| Fact | Source | Type | Importance |")
    decisions_idx = out.find("## Decisions")
    assert overview_idx < table_idx < decisions_idx


def test_compile_topic_marker_replaced_with_empty_when_no_facts() -> None:
    content = "before\n<<KEY_FACTS_TABLE>>\nafter"
    out = _splice_key_facts_table(content, [])
    assert "<<KEY_FACTS_TABLE>>" not in out
    assert out == "before\n\nafter"
