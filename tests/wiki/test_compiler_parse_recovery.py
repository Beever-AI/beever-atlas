"""Tests for Phase 1b: aggressive content-field recovery in _parse_llm_json.

Covers the failure mode where the LLM emits `{"content": "...", ...}` with
an unescaped `"` or raw control char *inside* the content string value,
which breaks the upstream control-char sanitizer (it toggles in_string
state when it encounters the stray quote).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from beever_atlas.wiki.compiler import _parse_llm_json, _recover_content_field


@pytest.fixture(autouse=True)
def _enable_hardening():
    """Force wiki_parse_hardening=True for these recovery tests."""
    with patch("beever_atlas.infra.config.get_settings") as gs:
        gs.return_value.wiki_parse_hardening = True
        yield


def test_recover_content_with_unescaped_quote() -> None:
    raw = '{"content": "This has an "unescaped" quote inside", "summary": "ok"}'
    recovered = _recover_content_field(raw)
    assert recovered is not None
    assert "unescaped" in recovered["content"]
    assert "This has an" in recovered["content"]
    assert recovered["summary"] == "ok"


def test_recover_content_with_raw_newline() -> None:
    raw = '{"content": "line one\nline two", "summary": "ok"}'
    recovered = _recover_content_field(raw)
    assert recovered is not None
    assert "line one" in recovered["content"]
    assert "line two" in recovered["content"]


def test_recover_content_with_both_failures() -> None:
    raw = '{"content": "has "quoted" bit\nand newline", "summary": "s"}'
    recovered = _recover_content_field(raw)
    assert recovered is not None
    assert "quoted" in recovered["content"]
    assert "and newline" in recovered["content"]


def test_recover_content_no_content_key() -> None:
    raw = '{"foo": "bar"}'
    recovered = _recover_content_field(raw)
    assert recovered is None


def test_recover_truncated_content() -> None:
    # Mid-string truncation: no closing `"`, no `}`. Interior must be >= 200
    # chars to clear the noise threshold.
    prose = (
        "some real content here with lots of text about topics and facts and "
        "sentences that clearly form prose longer than two hundred characters. "
        "We need at least this much to clear the threshold, and here is more "
        "padding to be safe and go well past the cutoff."
    )
    raw = '{"content": "' + prose
    recovered = _recover_content_field(raw)
    assert recovered is not None
    assert "some real content here" in recovered["content"]
    assert "padding to be safe" in recovered["content"]


def test_recover_truncated_too_short_returns_none() -> None:
    raw = '{"content": "tiny'
    recovered = _recover_content_field(raw)
    assert recovered is None


def test_parse_llm_json_reaches_content_recovery() -> None:
    # Unescaped quote + non-trivial prose: defeats fast path, brace-span retry,
    # and control-char sanitizer. Content-field recovery is the only path that
    # can salvage this.
    raw = '{"content": "The "tech-beever-atlas" channel is a place.", "summary": "ok"}'
    result = _parse_llm_json(raw)
    assert result is not None
    assert isinstance(result, dict)
    assert "tech-beever-atlas" in result["content"]
