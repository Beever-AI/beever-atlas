"""Tests for deterministic overview section splicing.

Overview generation is unstable — the LLM frequently omits Key Highlights,
Topics at a glance, Contributors, Tools, or Momentum. `_splice_overview_sections`
fills the gaps from channel-summary data so the Overview page renders
consistently regardless of LLM variance.
"""

from types import SimpleNamespace

from beever_atlas.wiki.compiler import _splice_overview_sections


def _make_summary(**overrides):
    defaults = dict(
        channel_name="tech",
        description="",
        themes="",
        momentum="Active work on ingestion pipeline.",
        top_people=[{"name": "Alice", "role": "lead"}, {"name": "Bob"}],
        media_count=7,
        date_range_start="2026-01-01",
        date_range_end="2026-02-01",
        recent_activity_summary={"highlights": ["New wiki shipped", "FAQ added"]},
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _make_cluster(title, mc=5, tags=None, cid="abc123"):
    return SimpleNamespace(
        id=cid, title=title, member_count=mc, topic_tags=tags or [], key_facts=[]
    )


def test_splice_adds_all_missing_sections():
    content = "## Overview\n\nA paragraph.\n\n## Concept Map\n```mermaid\ngraph TD\nA-->B\n```\n"
    out = _splice_overview_sections(
        content,
        channel_summary=_make_summary(),
        clusters=[_make_cluster("Pipeline", mc=10, tags=["ingestion"])],
        tech_data=[{"name": "Weaviate"}, {"name": "Slack"}],  # Slack is filtered
        project_data=[{"name": "Beever Atlas"}],
        decisions_count=4,
        skipped_topics=[],
    )
    assert "## Key Highlights" in out
    assert "| Decisions Made | 4 |" in out
    assert "| Resources Shared | 7 |" in out
    assert "## Topics at a glance" in out
    assert "**Pipeline** (10 memories)" in out
    assert "## Key contributors" in out
    assert "**Alice**" in out
    assert "## Tools & resources" in out
    assert "Weaviate" in out
    assert "Slack" not in out.split("## Tools & resources", 1)[1]
    assert "Beever Atlas" in out
    assert "## Recent momentum" in out
    assert "Active work on ingestion pipeline." in out


def test_splice_respects_existing_sections():
    content = (
        "## Overview\n\nIntro.\n\n"
        "## Key Highlights\n\n| Metric | Value |\n|---|---|\n| Topics | 99 |\n\n"
        "## Topics at a glance\n\n- existing\n"
    )
    out = _splice_overview_sections(
        content,
        channel_summary=_make_summary(),
        clusters=[_make_cluster("Pipeline")],
        tech_data=[],
        project_data=[],
        decisions_count=1,
        skipped_topics=[],
    )
    # Existing Key Highlights must not be duplicated
    assert out.count("## Key Highlights") == 1
    assert "| Topics | 99 |" in out
    assert out.count("## Topics at a glance") == 1
    # Contributors was missing → appended
    assert "## Key contributors" in out


def test_splice_skipped_topics_marked_brief():
    content = "## Overview\n\nText.\n"
    out = _splice_overview_sections(
        content,
        channel_summary=_make_summary(top_people=[]),
        clusters=[_make_cluster("OffTopic", mc=2)],
        tech_data=[],
        project_data=[],
        decisions_count=0,
        skipped_topics=[{"title": "OffTopic", "reason": "below_threshold"}],
    )
    assert "**OffTopic** (2 memories) (brief mention)" in out


def test_splice_skips_sections_with_no_data():
    content = "## Overview\n\nText.\n"
    out = _splice_overview_sections(
        content,
        channel_summary=_make_summary(top_people=[], momentum="", recent_activity_summary={}),
        clusters=[],
        tech_data=[],
        project_data=[],
        decisions_count=0,
        skipped_topics=[],
    )
    assert "## Key Highlights" in out  # always rendered
    assert "## Topics at a glance" not in out
    assert "## Key contributors" not in out
    assert "## Tools & resources" not in out
    assert "## Recent momentum" not in out


def test_splice_noop_on_empty_content():
    assert (
        _splice_overview_sections(
            "",
            channel_summary=_make_summary(),
            clusters=[],
            tech_data=[],
            project_data=[],
            decisions_count=0,
            skipped_topics=[],
        )
        == ""
    )


def test_splice_recognizes_heading_aliases():
    content = (
        "## Overview\n\nText.\n\n"
        "## Highlights\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
        "## Topics\n- t1\n\n"
        "## Contributors\n- Alice\n\n"
        "## Tools\n- x\n\n"
        "## Momentum\nOngoing.\n"
    )
    out = _splice_overview_sections(
        content,
        channel_summary=_make_summary(),
        clusters=[_make_cluster("P")],
        tech_data=[{"name": "Weaviate"}],
        project_data=[],
        decisions_count=2,
        skipped_topics=[],
    )
    # None of the canonical headings should have been duplicated
    assert out.count("## Key Highlights") == 0
    assert out.count("## Topics at a glance") == 0
    assert out.count("## Key contributors") == 0
    assert out.count("## Tools & resources") == 0
    assert out.count("## Recent momentum") == 0
