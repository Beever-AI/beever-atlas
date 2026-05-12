"""Tests for People & Experts page wikilink rewriting.

The People compiler uses the same ``_rewrite_topic_wikilinks`` post-processor
that the Glossary compiler uses. These tests verify that:

1. ``[[Topic Title]]`` wikilinks in People content (both the Contributors table
   "Topics Active In" column and per-profile "Topic activity:" bullets) resolve
   to ``/wiki/<slug>`` markdown links when the topic was compiled.
2. References to non-compiled (skipped/threshold-dropped) topics are stripped to
   plain text — no red broken links.
3. The rewrite is idempotent.
"""

import pytest

from beever_atlas.wiki.compiler import (
    _rewrite_topic_wikilinks,
    _topic_slug_for_title,
)


# ── helpers ─────────────────────────────────────────────────────────────────


def _make_people_content(topics: list[str]) -> str:
    """Build a minimal People-page markdown blob that references the given topics
    via ``[[Topic Title]]`` wikilinks in both table cells and profile bullets."""
    topic_cells = " · ".join(f"[[{t}]]" for t in topics)
    profile_bullets = "\n".join(f"- **Topic activity**: [[{t}]]: 5" for t in topics)
    return (
        "## Contributors Table\n\n"
        f"| Alice | Engineer | {topic_cells} | Built the thing | Chose Supabase |\n\n"
        "## Profiles\n\n"
        "### Alice\n\n"
        f"{profile_bullets}\n"
    )


# ── test 1: links to compiled topics resolve to /wiki/<slug> ─────────────────


def test_people_page_topic_links_resolve_to_compiled_slugs():
    """Wikilinks in People content for compiled topics must become real markdown
    links pointing at ``/wiki/<slug>`` — the same slug the topic compiler assigns."""
    topics = [
        "Discussion on Work, Food, and Transportation",
        "Work Arrangements and Office Safety Discussion",
        "Work From Home Policy Discussion Amidst COVID-19",
    ]
    content = _make_people_content(topics)
    out = _rewrite_topic_wikilinks(content, topics)

    for title in topics:
        slug = _topic_slug_for_title(title)
        assert f"[{title}](/wiki/{slug})" in out, (
            f"Expected compiled topic '{title}' to resolve to /wiki/{slug}"
        )
    # No raw [[...]] wikilinks remain for the compiled topics.
    for title in topics:
        assert f"[[{title}]]" not in out


# ── test 2: links to non-compiled topics become plain text ───────────────────


def test_people_page_drops_links_to_non_compiled_topics():
    """References to topics that were not compiled (threshold-dropped) must be
    rendered as plain text — never as ``[[...]]`` broken wikilinks."""
    compiled = "Work From Home Policy Discussion Amidst COVID-19"
    skipped = "Breakfast Inequality in the Office"

    content = (
        f"Topics Active In: [[{compiled}]], [[{skipped}]]\n"
        f"- **Topic activity**: [[{skipped}]]: 3 · [[{compiled}]]: 7\n"
    )
    out = _rewrite_topic_wikilinks(content, [compiled])

    slug = _topic_slug_for_title(compiled)
    # Compiled topic → real link.
    assert f"[{compiled}](/wiki/{slug})" in out
    # Skipped topic → plain text, no brackets.
    assert f"[[{skipped}]]" not in out
    assert skipped in out


# ── test 3: idempotency ──────────────────────────────────────────────────────


def test_people_page_rewrite_idempotent():
    """Running the rewrite twice must produce identical output — native markdown
    links (``[Title](/wiki/slug)``) must not be double-processed."""
    topics = [
        "Discussion on Work, Food, and Transportation",
        "Work Arrangements and Office Safety Discussion",
    ]
    content = _make_people_content(topics)
    once = _rewrite_topic_wikilinks(content, topics)
    twice = _rewrite_topic_wikilinks(once, topics)
    assert once == twice


# ── test 4: case-insensitive matching ────────────────────────────────────────


def test_people_page_rewrite_case_insensitive():
    """LLMs frequently lower-case part of a multi-word title. The rewriter must
    still resolve the link to the canonical slug."""
    title = "Work From Home Policy Discussion Amidst COVID-19"
    lowered = title.lower()
    content = f"- **Topic activity**: [[{lowered}]]: 12\n"
    out = _rewrite_topic_wikilinks(content, [title])
    slug = _topic_slug_for_title(title)
    assert f"](/wiki/{slug})" in out
    assert f"[[{lowered}]]" not in out


# ── test 5: profile section bullets also rewritten ───────────────────────────


def test_people_page_profile_bullets_also_rewritten():
    """The rewriter must cover both the Contributors table AND the per-profile
    'Topic activity:' bullets — they live in the same markdown blob."""
    compiled = "Discussion on Work, Food, and Transportation"
    skipped = "Some Skipped Topic"
    content = (
        "## Contributors Table\n\n"
        f"| Alice | Engineer | [[{compiled}]], [[{skipped}]] | ... | ... |\n\n"
        "## Profiles\n\n"
        "### Alice\n\n"
        f"- **Topic activity**: [[{compiled}]]: 8 · [[{skipped}]]: 2\n"
    )
    out = _rewrite_topic_wikilinks(content, [compiled])
    slug = _topic_slug_for_title(compiled)

    # Compiled topic resolved in both locations.
    assert out.count(f"[{compiled}](/wiki/{slug})") == 2
    # Skipped topic stripped in both locations.
    assert f"[[{skipped}]]" not in out
    assert out.count(skipped) == 2  # plain text kept in both places


# ── test 6: empty / None inputs are safe ────────────────────────────────────


@pytest.mark.parametrize("content", ["", None])
def test_people_page_rewrite_empty_content_safe(content):
    """Passing empty or None content must not raise."""
    result = _rewrite_topic_wikilinks(content, ["Some Topic"])  # type: ignore[arg-type]
    assert result == content
