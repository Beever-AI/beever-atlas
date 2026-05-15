"""Tests for ``_strip_safety_markers`` — the prompt-safety wrapper
stripper that prevents ``<untrusted>`` tags from leaking into
frontend-rendered fact text.
"""

from __future__ import annotations

from beever_atlas.wiki.modules._text_utils import _strip_safety_markers


# ---------------------------------------------------------------------------
# Stripping each known safety wrapper tag
# ---------------------------------------------------------------------------


def test_strips_untrusted_open_close_pair() -> None:
    src = "<untrusted>\nhello world\n</untrusted>"
    assert _strip_safety_markers(src) == "hello world"


def test_strips_sanitized_open_close_pair() -> None:
    src = "<sanitized>\nfoo\n</sanitized>"
    assert _strip_safety_markers(src) == "foo"


def test_strips_external_open_close_pair() -> None:
    src = "<external>\nbar\n</external>"
    assert _strip_safety_markers(src) == "bar"


def test_strips_lone_open_tag() -> None:
    """Some upstream paths emit only the opening marker without a
    matching close — strip the lone tag and return the body."""
    assert _strip_safety_markers("<untrusted> partial body") == "partial body"


def test_strips_lone_close_tag() -> None:
    assert _strip_safety_markers("orphan body </untrusted>") == "orphan body"


def test_strips_multiple_wrappers_full() -> None:
    """All known tags get stripped regardless of nesting / ordering."""
    src = "<untrusted>part1</untrusted> <sanitized>part2</sanitized> <external>part3</external>"
    out = _strip_safety_markers(src)
    assert "<untrusted>" not in out
    assert "</untrusted>" not in out
    assert "<sanitized>" not in out
    assert "</sanitized>" not in out
    assert "<external>" not in out
    assert "</external>" not in out
    assert "part1" in out
    assert "part2" in out
    assert "part3" in out


# ---------------------------------------------------------------------------
# Idempotence — calling twice produces the same result as once.
# ---------------------------------------------------------------------------


def test_idempotent_on_already_clean_text() -> None:
    text = "Just plain text."
    assert _strip_safety_markers(_strip_safety_markers(text)) == text


def test_idempotent_on_wrapped_text() -> None:
    src = "<untrusted>\nclaim body\n</untrusted>"
    once = _strip_safety_markers(src)
    twice = _strip_safety_markers(once)
    assert once == twice == "claim body"


# ---------------------------------------------------------------------------
# No false positives on legitimate ``<`` / HTML-ish input
# ---------------------------------------------------------------------------


def test_preserves_inequality_in_code_text() -> None:
    src = "if x < 5 and y > 10: pass"
    assert _strip_safety_markers(src) == src


def test_preserves_html_in_fact_text() -> None:
    src = "Use <strong>bold</strong> for emphasis"
    assert _strip_safety_markers(src) == src


def test_preserves_unrelated_tag_lookalikes() -> None:
    src = "<not-a-safety-tag>keep me</not-a-safety-tag>"
    assert _strip_safety_markers(src) == src


def test_preserves_jsx_like_text() -> None:
    src = "<MyComponent prop={value} />"
    assert _strip_safety_markers(src) == src


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_returns_empty_for_none() -> None:
    assert _strip_safety_markers(None) == ""


def test_returns_empty_for_empty_string() -> None:
    assert _strip_safety_markers("") == ""


def test_returns_empty_for_whitespace_only() -> None:
    assert _strip_safety_markers("   \n\t  ") == ""


def test_coerces_non_string_input() -> None:
    assert _strip_safety_markers(123) == "123"


def test_strips_trailing_whitespace() -> None:
    src = "<untrusted>  body with spaces  </untrusted>"
    assert _strip_safety_markers(src) == "body with spaces"


def test_only_inner_whitespace_collapses_to_empty() -> None:
    src = "<untrusted></untrusted>"
    assert _strip_safety_markers(src) == ""


def test_real_world_user_facing_bug_example() -> None:
    """Reproduces the exact user-reported bug shape: a fact text that
    rendered as ``<untrusted> Ronald Ng (via Jack Ng)...`` in a
    ``KeyFactsModule`` card. The stripped output must NOT contain
    ``<untrusted>`` anywhere."""
    src = (
        "<untrusted>\n"
        "Ronald Ng (via Jack Ng) performed code analysis using a "
        "shared workspace.\n"
        "</untrusted>"
    )
    out = _strip_safety_markers(src)
    assert "<untrusted>" not in out
    assert "</untrusted>" not in out
    assert out.startswith("Ronald Ng")
    assert out.endswith("workspace.")
