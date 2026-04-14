"""Tests for Phase 1a: control-char sanitizer in _parse_llm_json."""

from __future__ import annotations

import json

import pytest

from beever_atlas.wiki.compiler import _escape_control_chars_inside_strings, _parse_llm_json


def test_parse_llm_json_sanitizer_recovers_raw_newline_in_string() -> None:
    """A literal newline byte inside a JSON string value must be recovered."""
    # Construct JSON with a raw \n inside the "content" string value.
    # json.loads rejects this, but our sanitizer should fix it.
    raw = '{"content": "line one\nline two", "summary": "test"}'
    # Verify that plain json.loads rejects it.
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    # Our sanitizer should recover it.
    result = _parse_llm_json(raw)
    assert result is not None, "sanitizer failed to recover raw newline"
    assert isinstance(result, dict)
    assert "line one" in result["content"]
    assert "line two" in result["content"]


def test_parse_llm_json_sanitizer_preserves_already_escaped() -> None:
    """Valid JSON with \\n escape sequences must round-trip unchanged."""
    payload = {"content": "line one\\nline two", "summary": "ok"}
    raw = json.dumps(payload)
    result = _parse_llm_json(raw)
    assert result is not None
    assert result["content"] == "line one\\nline two"


def test_parse_llm_json_sanitizer_does_not_escape_outside_strings() -> None:
    """Newlines between JSON keys (structural whitespace) must still parse."""
    raw = '{\n  "content": "hello world",\n  "summary": "ok"\n}'
    result = _parse_llm_json(raw)
    assert result is not None
    assert result["content"] == "hello world"


def test_escape_control_chars_preserves_unicode() -> None:
    """Non-ASCII content inside strings must survive the sanitizer."""
    raw = '{"content": "你好世界 hello", "summary": "cjk"}'
    result = _parse_llm_json(raw)
    assert result is not None
    assert "你好世界" in result["content"]


def test_escape_control_chars_handles_tab_in_string() -> None:
    """A literal tab inside a JSON string value should be escaped."""
    raw = '{"content": "col1\tcol2", "summary": "tab"}'
    with pytest.raises(json.JSONDecodeError):
        json.loads(raw)
    result = _parse_llm_json(raw)
    assert result is not None
    assert "col1" in result["content"]
    assert "col2" in result["content"]


def test_escape_control_chars_handles_raw_carriage_return() -> None:
    """A literal \\r inside a JSON string should be escaped."""
    raw = '{"content": "line\rend", "summary": "cr"}'
    result = _parse_llm_json(raw)
    assert result is not None
    assert result is not None


def test_escape_control_chars_inside_strings_identity_on_clean_json() -> None:
    """Clean JSON with no control chars must pass through without change."""
    clean = '{"content": "normal text here", "summary": "ok"}'
    assert _escape_control_chars_inside_strings(clean) == clean


def test_parse_llm_json_returns_none_on_total_garbage() -> None:
    """Completely unrecoverable input must return None."""
    assert _parse_llm_json("not json at all !!!") is None


def test_parse_llm_json_handles_code_fenced_json() -> None:
    """JSON wrapped in ```json ... ``` fences must parse."""
    raw = '```json\n{"content": "hello", "summary": "test"}\n```'
    result = _parse_llm_json(raw)
    assert result is not None
    assert result["content"] == "hello"
