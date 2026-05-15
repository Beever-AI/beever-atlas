"""Tests for the <<CHILDREN_TOC>> marker rendering used by folder pages."""

from __future__ import annotations

from beever_atlas.wiki.render import (
    CHILDREN_TOC_MARKER,
    apply_children_toc_marker,
    render_children_toc,
)


def test_render_children_toc_basic() -> None:
    """A list of children renders as Markdown bullets with links."""
    rendered = render_children_toc(
        [
            {"title": "Auth", "slug": "topic-auth"},
            {"title": "RBAC", "slug": "topic-rbac"},
        ]
    )
    assert rendered.splitlines() == [
        "- [Auth](/wiki/topic-auth)",
        "- [RBAC](/wiki/topic-rbac)",
    ]


def test_render_children_toc_with_summary() -> None:
    """Summaries are appended after an em-dash, truncated near the
    200-char budget at a word boundary, with an ellipsis sentinel
    when the original summary exceeds the budget."""
    # 250 chars of single-letter words so there's a space at every
    # position — guarantees the word-boundary path fires (not the
    # all-no-space fallback that bare-truncates to 200 + ellipsis).
    long_summary = ("x " * 125).rstrip()  # 249 chars
    rendered = render_children_toc([{"title": "T", "slug": "t", "summary": long_summary}])
    assert rendered.startswith("- [T](/wiki/t) — ")
    # Truncated content followed by ellipsis sentinel.
    assert rendered.endswith("…")
    # Total rendered line stays at or under the prefix + budget + ellipsis.
    assert len(rendered) <= len("- [T](/wiki/t) — ") + 200 + 1


def test_render_children_toc_does_not_truncate_mid_word() -> None:
    """Regression: the renderer used to cut at a fixed 140-char
    boundary regardless of word boundaries, producing fragments like
    "...architectural discussions, platfo" on real folder pages.
    Verify the new word-boundary trim never leaves a half-word at
    the end before the ellipsis."""
    long_real = (
        "This folder details the ongoing development and integration "
        "efforts for the Beever Atlas project, covering architectural "
        "discussions, platform integrations like Mattermost and Microsoft "
        "Teams, and documentation strategy across the project."
    )
    rendered = render_children_toc(
        [{"title": "Dev & Integration", "slug": "dev-int", "summary": long_real}]
    )
    # Strip prefix to inspect just the summary fragment.
    summary_part = rendered.split(" — ", 1)[1]
    # The fragment must end on a sentence boundary OR a complete word
    # immediately before "…" — never in the middle of a word.
    if summary_part.endswith("…"):
        last_word = summary_part[:-1].rstrip().split(" ")[-1]
        # A complete word means: ends with letters (no leading partial
        # syllable like "platfo"). Loose check: the last word, if
        # appearing in the original, must appear as a whole word.
        assert f" {last_word} " in f" {long_real} " or long_real.endswith(last_word), (
            f"Truncated mid-word: ...{summary_part[-30:]}"
        )
    else:
        # Otherwise the summary fits within budget — fragment must end
        # at a sentence terminator.
        assert summary_part[-1] in ".!?", (
            f"Untruncated summary should end on punctuation, got ...{summary_part[-30:]}"
        )


def test_render_children_toc_handles_missing_slug() -> None:
    """When slug is missing, render the title as plain text (no link)."""
    rendered = render_children_toc([{"title": "Plain"}])
    assert rendered == "- Plain"


def test_render_children_toc_empty() -> None:
    assert render_children_toc([]) == ""


def test_apply_marker_replaces_inline() -> None:
    """Marker in the middle of content is replaced with the rendered TOC."""
    content = "## Intro\nSome prose.\n\n" + CHILDREN_TOC_MARKER + "\n\n## Threads\nSome more prose."
    out = apply_children_toc_marker(
        content, [{"title": "A", "slug": "a"}, {"title": "B", "slug": "b"}]
    )
    assert "- [A](/wiki/a)" in out
    assert "- [B](/wiki/b)" in out
    assert CHILDREN_TOC_MARKER not in out
    assert "## Threads" in out


def test_apply_marker_appends_fallback_when_marker_missing() -> None:
    """If the LLM forgot the marker, append a default section at the end."""
    content = "## Intro\nProse without a marker."
    out = apply_children_toc_marker(content, [{"title": "A", "slug": "a"}])
    assert out.startswith("## Intro\nProse without a marker.")
    assert "## Pages in this folder" in out
    assert "- [A](/wiki/a)" in out


def test_apply_marker_strips_marker_line_when_no_children() -> None:
    """Marker line dropped when children is empty (no useless heading)."""
    content = "## Intro\n\n" + CHILDREN_TOC_MARKER + "\n\n## After"
    out = apply_children_toc_marker(content, [])
    assert CHILDREN_TOC_MARKER not in out
    assert "## After" in out
    # No fallback heading either.
    assert "## Pages in this folder" not in out


def test_apply_marker_no_op_when_no_marker_no_children() -> None:
    """Empty children + no marker is a no-op."""
    content = "## Intro\nProse."
    assert apply_children_toc_marker(content, []) == content
