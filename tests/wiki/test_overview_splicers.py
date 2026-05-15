"""Tests for the per-section overview splicers (Hybrid B).

`_splice_recent_updates`, `_splice_project_status`, and
`_splice_core_discussions` post-process the LLM-generated Overview
body to surface three new H2 sections deterministically. They must
be idempotent (skip when the localized H2 already exists), skip
when the input data is empty, and use the localized translation
for the heading.
"""

from beever_atlas.wiki.compiler import (
    _splice_core_discussions,
    _splice_project_status,
    _splice_recent_updates,
)


def test_recent_updates_splices_when_activity_present():
    body = "## Overview\n\nintro\n"
    out = _splice_recent_updates(
        body,
        recent_activity_summary={
            "facts_added_7d": 12,
            "decisions_added_7d": 3,
            "new_topics": ["Auth Roadmap"],
            "updated_topics": ["JWT Migration"],
            "highlights": [
                "Shipped wiki splicers",
                {"memory_text": "Adopted JWT for session auth.", "author_name": "Alan"},
            ],
        },
        lang="en",
    )
    assert "## Recent Updates" in out
    assert "12 new memories" in out
    assert "3 decisions" in out
    assert "Auth Roadmap" in out
    assert "JWT Migration" in out
    # Bulleted list with at least one item.
    assert out.count("\n- ") >= 1


def test_recent_updates_skips_when_empty():
    body = "## Overview\n\nintro\n"
    out = _splice_recent_updates(body, recent_activity_summary={}, lang="en")
    assert out == body
    out2 = _splice_recent_updates(body, recent_activity_summary=None, lang="en")  # type: ignore[arg-type]
    assert out2 == body


def test_project_status_combines_momentum_and_team_dynamics():
    body = "## Overview\n\nintro\n"
    out = _splice_project_status(
        body,
        momentum="Active work on ingestion pipeline.",
        team_dynamics="Alan drives architecture; Bob owns ops.",
        lang="en",
    )
    assert "## Project Status & Progress" in out
    assert out.count("## Project Status & Progress") == 1
    assert "Active work on ingestion pipeline." in out
    assert "Alan drives architecture" in out
    # Single combined section.
    assert "**Momentum**" in out
    assert "**Team dynamics**" in out


def test_project_status_skips_when_both_empty():
    body = "## Overview\n\nintro\n"
    out = _splice_project_status(body, momentum="", team_dynamics="", lang="en")
    assert out == body
    out2 = _splice_project_status(body, momentum="   ", team_dynamics=None, lang="en")  # type: ignore[arg-type]
    assert out2 == body


def test_core_discussions_renders_top_decisions_and_quotes():
    body = "## Overview\n\nintro\n"
    out = _splice_core_discussions(
        body,
        top_decisions=[
            {"name": "Adopt JWT", "decided_by": "Alan", "date": "2026-04-15"},
            {"name": "Drop SAML", "decided_by": "Bob", "date": "2026-04-20"},
            {"name": "Use Postgres", "decided_by": "Carol", "date": "2026-03-10"},
            # 4th decision should be capped out.
            {"name": "Switch to OAuth", "decided_by": "Dan"},
        ],
        cited_facts=[
            {"index": 1, "author": "Alan", "excerpt": "JWT is faster."},
            {"index": 2, "author": "Bob", "excerpt": "SAML is too heavy."},
            {"index": 3, "author": "Carol", "excerpt": "Postgres scales fine."},
            # 4th fact should be capped out.
            {"index": 4, "author": "Dan", "excerpt": "OAuth is industry standard."},
        ],
        lang="en",
    )
    assert "## Core Discussions" in out
    # Decisions capped at 3.
    assert "Adopt JWT" in out
    assert "Drop SAML" in out
    assert "Use Postgres" in out
    assert "Switch to OAuth" not in out
    # Quotes capped at 3 with index-based citation markers.
    assert "[1]" in out
    assert "[2]" in out
    assert "[3]" in out
    assert "[4]" not in out


def test_core_discussions_skips_when_both_empty():
    body = "## Overview\n\nintro\n"
    out = _splice_core_discussions(body, top_decisions=[], cited_facts=[], lang="en")
    assert out == body
    out2 = _splice_core_discussions(body, top_decisions=None, cited_facts=None, lang="en")  # type: ignore[arg-type]
    assert out2 == body


def test_idempotent_when_section_already_present():
    body = "## Overview\n\nintro\n\n## Recent Updates\n\n- Already shipped this section.\n"
    out = _splice_recent_updates(
        body,
        recent_activity_summary={"facts_added_7d": 5, "highlights": ["NEW HIGHLIGHT"]},
        lang="en",
    )
    # Heading must not be duplicated.
    assert out.count("## Recent Updates") == 1
    # Existing content preserved; new auto-content NOT injected.
    assert "Already shipped this section." in out
    assert "NEW HIGHLIGHT" not in out

    # Idempotency for project status too.
    body2 = "## Overview\n\nintro\n\n## Project Status & Progress\n\nexisting prose\n"
    out2 = _splice_project_status(
        body2,
        momentum="new momentum",
        team_dynamics="new dynamics",
        lang="en",
    )
    assert out2.count("## Project Status & Progress") == 1
    assert "new momentum" not in out2

    # Idempotency for core discussions.
    body3 = "## Overview\n\nintro\n\n## Core Discussions\n\nexisting prose\n"
    out3 = _splice_core_discussions(
        body3,
        top_decisions=[{"name": "X", "decided_by": "A"}],
        cited_facts=[{"index": 1, "author": "A", "excerpt": "Y"}],
        lang="en",
    )
    assert out3.count("## Core Discussions") == 1
    assert "decided" not in out3 or "**X**" not in out3


def test_localizes_heading_in_japanese():
    body = "## 概要\n\n紹介\n"
    out_ru = _splice_recent_updates(
        body,
        recent_activity_summary={"facts_added_7d": 3},
        lang="ja",
    )
    assert "## 最近の更新" in out_ru
    # English title must NOT appear.
    assert "## Recent Updates" not in out_ru

    out_ps = _splice_project_status(
        body,
        momentum="進捗あり",
        team_dynamics="チームは順調",
        lang="ja",
    )
    assert "## プロジェクト状況と進捗" in out_ps
    assert "## Project Status" not in out_ps

    out_cd = _splice_core_discussions(
        body,
        top_decisions=[{"name": "JWT 採用", "decided_by": "Alan"}],
        cited_facts=[{"index": 1, "author": "Alan", "excerpt": "JWT は速い"}],
        lang="ja",
    )
    assert "## 主要な議論" in out_cd
    assert "## Core Discussions" not in out_cd


def test_core_discussions_strips_untrusted_wrappers():
    """``cited_facts_for_prompt`` wraps every excerpt with
    ``<untrusted>...</untrusted>`` for LLM-context defense. Those
    tags must NEVER reach the rendered body — a previous regression
    rendered them literally to the user."""
    body = "## Overview\n\nintro\n"
    out = _splice_core_discussions(
        body,
        top_decisions=[],
        cited_facts=[
            {
                "index": 1,
                "author": "Thomas",
                "excerpt": (
                    "<untrusted> Thomas reported that Claude was asked to "
                    "use ElevenLabs and Vercel as references </untrusted>"
                ),
            },
            {
                "index": 2,
                "author": "Jacky",
                "memory_text": (
                    "<untrusted><br>Jacky stated the security check task "
                    "encompasses code conversion<br></untrusted>"
                ),
            },
        ],
        lang="en",
    )
    assert "<untrusted>" not in out
    assert "</untrusted>" not in out
    assert "Thomas reported that Claude" in out
    assert "Jacky stated the security check" in out


def test_skips_when_locale_lacks_translation_entry():
    """When ``lang`` is among supported_languages but missing from
    ``WIKI_PAGE_TITLES`` (e.g. ``pt`` / ``ru`` / ``ar``), the splicer
    must skip rather than splice an English heading into a body the
    LLM already translated to the target language."""
    body = "# Overview\n\nSome translated body.\n"
    out_ru = _splice_recent_updates(
        body,
        recent_activity_summary={"facts_added_7d": 5, "decisions_added_7d": 2},
        lang="pt",
    )
    assert out_ru == body, "splicer must not insert when locale lacks translations"

    out_ps = _splice_project_status(
        body,
        momentum="Active work",
        team_dynamics="Bob driving",
        lang="ru",
    )
    assert out_ps == body

    out_cd = _splice_core_discussions(
        body,
        top_decisions=[{"name": "Ship feature flag default OFF"}],
        cited_facts=[{"index": 1, "memory_text": "REST is sufficient for v1"}],
        lang="ar",
    )
    assert out_cd == body


def test_project_status_dedupes_momentum_with_legacy_recent_momentum():
    """``_splice_overview_sections`` already emits ``## Recent momentum``
    from ``summary.momentum``. When that section is in the body, the
    Project Status splicer must drop its momentum bullet (avoid two
    momentum lines under different headings) but keep team_dynamics."""
    body = "# Overview\n\n## Recent momentum\n\nActive work on ingestion pipeline.\n"
    out = _splice_project_status(
        body,
        momentum="Active work on ingestion pipeline.",
        team_dynamics="Alan drives architecture; Bob owns ops.",
        lang="en",
    )
    # Project Status section must appear (team_dynamics is non-empty).
    assert "## Project Status" in out
    assert "Alan drives architecture" in out
    # But the Momentum bullet must NOT be duplicated under it.
    assert "**Momentum** —" not in out


def test_project_status_skips_when_dedup_leaves_nothing_to_render():
    """If ``_splice_overview_sections`` already emitted Recent momentum
    AND team_dynamics is empty, Project Status has nothing left to
    render → skip entirely."""
    body = "# Overview\n\n## Recent momentum\n\nActive work on ingestion pipeline.\n"
    out = _splice_project_status(
        body,
        momentum="Active work on ingestion pipeline.",
        team_dynamics="",
        lang="en",
    )
    assert out == body


def test_idempotent_against_decorated_heading():
    """The LLM may emit ``## **Recent Updates**`` (bold), ``## 1.
    Recent Updates`` (numbered), ``## Recent Updates 🚀`` (emoji), or
    ``## Recent Updates {#anchor}`` (anchor). Each form must be
    detected so the splicer doesn't double-insert the section."""
    for decorated in (
        "# Overview\n\n## **Recent Updates**\n\n- something\n",
        "# Overview\n\n## *Recent Updates*\n\n- something\n",
        "# Overview\n\n## 1. Recent Updates\n\n- something\n",
        "# Overview\n\n## Recent Updates 🚀\n\n- something\n",
        "# Overview\n\n## Recent Updates {#anchor}\n\n- something\n",
    ):
        out = _splice_recent_updates(
            decorated,
            recent_activity_summary={"facts_added_7d": 5},
            lang="en",
        )
        assert out == decorated, (
            f"splicer must detect decorated heading, but it inserted on: {decorated!r}"
        )
