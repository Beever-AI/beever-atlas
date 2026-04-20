"""Tests for deterministic Sub-Topic section splicing.

Sub-topic pages sometimes render as TL;DR + concept diagram only, with no
Key Facts table and no Overview. The splicer injects both deterministically
so every sub-page has substantive body content.
"""

from types import SimpleNamespace

from beever_atlas.wiki.compiler import _splice_subtopic_sections


def _fact(text, author, fact_type="observation", importance="high"):
    return SimpleNamespace(
        memory_text=text,
        author_name=author,
        fact_type=fact_type,
        importance=importance,
        quality_score=0.9,
        message_ts="2026-04-01",
    )


def _key_fact_dict(text, author, fact_type="observation", importance="high"):
    return {
        "memory_text": text,
        "author_name": author,
        "fact_type": fact_type,
        "importance": importance,
        "quality_score": 0.9,
    }


def test_splice_adds_key_facts_and_overview_when_missing():
    content = "**TL;DR bold line.**\n\n## Concept diagram\n```mermaid\ngraph TD\nA-->B\n```\n"
    facts = [_fact("Fact 1", "Alice"), _fact("Fact 2", "Bob")]
    kf = [_key_fact_dict("Fact 1", "Alice"), _key_fact_dict("Fact 2", "Bob")]
    out = _splice_subtopic_sections(content, "Sub A", facts, "Parent X", kf)
    assert "## Key Facts" in out
    assert "## Overview" in out
    assert "Parent X" in out
    assert "Sub A" in out
    assert "2 related memories" in out
    assert "Alice" in out and "Bob" in out


def test_splice_preserves_existing_sections():
    content = (
        "**TL;DR.**\n\n"
        "## Key Facts\n\n| F | S | T | I |\n|---|---|---|---|\n| x | y | z | w |\n\n"
        "## Overview\n\nAlready present.\n"
    )
    facts = [_fact("F", "A")]
    out = _splice_subtopic_sections(content, "Sub", facts, "Parent", [_key_fact_dict("F", "A")])
    assert out.count("## Key Facts") == 1
    assert out.count("## Overview") == 1
    assert "Already present." in out


def test_splice_treats_details_as_overview_alias():
    content = "**TL;DR.**\n\n## Key Facts\n\n| F | S | T | I |\n|---|---|---|---|\n| x | y | z | w |\n\n## Details\n\nBody.\n"
    out = _splice_subtopic_sections(
        content, "Sub", [_fact("F", "A")], "Parent", [_key_fact_dict("F", "A")]
    )
    # Details satisfies the overview alias — no duplicate ## Overview appended.
    assert out.count("## Overview") == 0


def test_splice_noop_on_empty_or_no_data():
    assert _splice_subtopic_sections("", "s", [], "p", []) == ""
    # No facts → splice skips Key Facts but Overview also skips (count=0).
    out = _splice_subtopic_sections("**TL;DR.**\n", "s", [], "p", [])
    assert "## Key Facts" not in out
    assert "## Overview" not in out


def test_splice_authors_truncated_after_three():
    facts = [_fact(f"f{i}", f"A{i}") for i in range(6)]
    kf = [_key_fact_dict(f"f{i}", f"A{i}") for i in range(6)]
    out = _splice_subtopic_sections("**TL;DR.**\n", "S", facts, "P", kf)
    assert "and 3 others" in out
