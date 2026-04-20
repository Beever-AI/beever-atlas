"""Tests for the restructured Recent Activity fallback renderer.

The fallback replaces raw "Topic — N memories (timestamp)" bullets with a
summary-stats table, optional area chart, daily breakdown, top contributors,
and a topics-with-recent-activity table when no facts exist.
"""

from types import SimpleNamespace

from beever_atlas.wiki.compiler import _activity_fallback


def _fact(text, author, ts, fact_type="observation"):
    return SimpleNamespace(
        memory_text=text,
        author_name=author,
        message_ts=ts,
        fact_type=fact_type,
    )


def _cluster(title, mc, end):
    return SimpleNamespace(title=title, member_count=mc, date_range_end=end)


def test_rich_output_with_facts():
    facts = [
        _fact("Shipped wiki v2.", "Alice", "2026-04-10T12:00:00+00:00", "decision"),
        _fact("Added splice helper.", "Alice", "2026-04-10T13:00:00+00:00"),
        _fact("FAQ tweak landed.", "Bob", "2026-04-11T09:00:00+00:00"),
        _fact("Glossary refactor planned.", "Bob", "2026-04-12T11:00:00+00:00", "decision"),
    ]
    content, summary = _activity_fallback(facts, {}, [])
    assert "## Summary" in content
    assert "| Memories added | 4 |" in content
    assert "| Decisions | 2 |" in content
    assert "| Contributors | 2 |" in content
    # 3 distinct days → chart renders
    assert "## Activity Chart" in content
    assert "```chart" in content
    assert "## Daily Breakdown" in content
    assert "### 2026-04-12" in content  # most recent first
    assert content.find("### 2026-04-12") < content.find("### 2026-04-11")
    assert "## Top Contributors" in content
    assert "**Alice** — 2 memories" in content
    assert "4 memories" in summary
    assert "2 decisions" in summary


def test_no_chart_when_few_days():
    facts = [
        _fact("One.", "A", "2026-04-10T12:00:00+00:00"),
        _fact("Two.", "A", "2026-04-10T13:00:00+00:00"),
    ]
    content, _ = _activity_fallback(facts, {}, [])
    assert "## Summary" in content
    assert "## Activity Chart" not in content
    assert "## Daily Breakdown" in content


def test_clusters_path_renders_table():
    clusters = [
        _cluster("Alpha", 12, "2026-04-10"),
        _cluster("Beta", 3, "2026-04-05"),
    ]
    content, summary = _activity_fallback([], {}, clusters)
    assert "## Topics with Recent Activity" in content
    assert "| Topic | Memories | Last Update |" in content
    assert "| Alpha | 12 | 2026-04-10 |" in content
    # Most-recent first
    assert content.find("Alpha") < content.find("Beta")


def test_empty_inputs_produces_placeholder():
    content, summary = _activity_fallback([], {}, [])
    assert content.strip()
    assert "Auto-generated" in summary or "recent activity" in summary.lower()


def test_timestamp_normalization():
    facts = [_fact("X.", "A", "2026-04-10T12:00:00.000Z")]
    content, _ = _activity_fallback(facts, {}, [])
    # Raw timestamp must not appear; normalized date does
    assert "2026-04-10T12" not in content
    assert "2026-04-10" in content
