"""Tests for Phase 1c: degenerate-content guard in _is_degenerate_content."""

from __future__ import annotations

from beever_atlas.wiki.compiler import _is_degenerate_content


def test_is_degenerate_content_dash_wall() -> None:
    """6 consecutive GFM separator rows → (True, 'dash wall').

    We prepend a large prose block so the alnum ratio check passes first,
    and the dash-wall check is what fires.
    """
    # Enough prose to push alnum ratio above 0.2 overall.
    prose = "This section contains important information about the architecture decisions made by the team.\n"
    sep = "| --- | --- | --- |"
    # 6 consecutive GFM separator rows after the prose.
    rows = [prose] + [sep] * 6
    content = "\n".join(rows)
    # Verify the alnum ratio is above threshold so dash-wall check runs.
    alnum = sum(1 for c in content if c.isalnum())
    assert alnum / len(content) >= 0.2, "test setup error: alnum ratio too low"
    is_degen, reason = _is_degenerate_content(content)
    assert is_degen is True
    assert reason == "dash wall"


def test_is_degenerate_content_low_alnum() -> None:
    """Content with 90% pipe/dash characters → (True, 'low alnum ratio')."""
    # 90% pipes and dashes, 10% alphanumeric
    content = "|" * 90 + "a" * 10
    # Must be at least 80 chars to not trigger "too short".
    assert len(content) >= 80
    is_degen, reason = _is_degenerate_content(content)
    assert is_degen is True
    assert reason == "low alnum ratio"


def test_is_degenerate_content_too_short() -> None:
    """Content under 80 chars → (True, 'too short')."""
    content = "short"
    is_degen, reason = _is_degenerate_content(content)
    assert is_degen is True
    assert reason == "too short"


def test_is_degenerate_content_visuals_only() -> None:
    """Content that is only fenced code blocks with no prose → (True, 'visuals only')."""
    content = "```mermaid\ngraph TD\n  A --> B\n```\n" * 3
    # Make it long enough to pass the "too short" check.
    content = content + " " * 50
    is_degen, reason = _is_degenerate_content(content)
    # After stripping fenced blocks, no alphanumeric chars remain.
    assert is_degen is True
    assert reason == "visuals only"


def test_is_degenerate_content_healthy() -> None:
    """Real markdown prose → (False, '')."""
    content = (
        "## Overview\n\n"
        "The team has been working on the API redesign for several weeks. "
        "Alice proposed switching to REST principles to improve client compatibility "
        "and reduce integration complexity. The new design follows OpenAPI 3.0 specification "
        "and uses URL-based versioning for clarity.\n\n"
        "## Key Points\n\n"
        "- REST architecture adopted\n"
        "- OpenAPI 3.0 spec\n"
        "- URL versioning preferred\n"
    )
    is_degen, reason = _is_degenerate_content(content)
    assert is_degen is False
    assert reason == ""


def test_is_degenerate_content_exactly_four_sep_rows_not_degenerate() -> None:
    """Exactly 4 consecutive separator rows is below the threshold of 5."""
    header = "| Column A | Column B |"
    sep = "| --- | --- |"
    content = "\n".join([header] + [sep] * 4) + "\nsome real prose here " + "a" * 80
    is_degen, reason = _is_degenerate_content(content)
    # 4 rows is fine; not a dash-wall (threshold is >= 5).
    assert not (is_degen and reason == "dash wall")


def test_is_degenerate_content_five_sep_rows_triggers() -> None:
    """Exactly 5 consecutive separator rows meets the threshold."""
    prose = (
        "This section contains important architectural decisions made by the engineering team.\n"
    )
    sep = "| --- | --- |"
    content = "\n".join([prose] + [sep] * 5)
    alnum = sum(1 for c in content if c.isalnum())
    assert alnum / len(content) >= 0.2, "test setup error: alnum ratio too low"
    is_degen, reason = _is_degenerate_content(content)
    assert is_degen is True
    assert reason == "dash wall"
