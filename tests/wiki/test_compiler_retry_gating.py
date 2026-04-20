"""Tests for Phase 1 (2g): retry gating in _call_llm."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from beever_atlas.wiki.compiler import WikiCompiler


def _make_compiler() -> WikiCompiler:
    """Return a WikiCompiler with a mocked LLM provider."""
    from unittest.mock import MagicMock

    provider = MagicMock()
    provider.get_model_string.return_value = "gemini-2.5-flash"
    with patch("beever_atlas.wiki.compiler.get_llm_provider", return_value=provider):
        return WikiCompiler()


@pytest.mark.asyncio
async def test_skip_retry_on_short_raw() -> None:
    """If the raw response is < 100 chars on attempt 0, do not retry — exactly one call."""
    compiler = _make_compiler()
    call_count = 0

    async def _short_response(self_inner, prompt: str, temperature: float = 0.2, **kwargs) -> str:
        nonlocal call_count
        call_count += 1
        return "nope"

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_parse_hardening = True
        with patch.object(WikiCompiler, "_llm_generate_json", _short_response):
            result = await compiler._call_llm("test prompt", max_retries=1)

    assert call_count == 1, f"Expected exactly 1 LLM call, got {call_count}"
    assert result.content == ""


@pytest.mark.asyncio
async def test_skip_retry_on_safety_block() -> None:
    """If the raw response looks like a safety block, do not retry — exactly one call."""
    compiler = _make_compiler()
    call_count = 0

    async def _safety_response(self_inner, prompt: str, temperature: float = 0.2, **kwargs) -> str:
        nonlocal call_count
        call_count += 1
        return "I can't help with that. This request violates our usage policy."

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_parse_hardening = True
        with patch.object(WikiCompiler, "_llm_generate_json", _safety_response):
            result = await compiler._call_llm("test prompt", max_retries=1)

    assert call_count == 1, f"Expected exactly 1 LLM call, got {call_count}"
    assert result.content == ""


@pytest.mark.asyncio
async def test_skip_retry_on_cannot_prefix() -> None:
    """'I cannot' prefix triggers no-retry."""
    compiler = _make_compiler()
    call_count = 0

    async def _cannot_response(self_inner, prompt: str, temperature: float = 0.2, **kwargs) -> str:
        nonlocal call_count
        call_count += 1
        return "I cannot generate content about this topic as it violates policy."

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_parse_hardening = True
        with patch.object(WikiCompiler, "_llm_generate_json", _cannot_response):
            await compiler._call_llm("test prompt", max_retries=1)

    assert call_count == 1


@pytest.mark.asyncio
async def test_retry_on_parse_failure_without_hardening() -> None:
    """Without hardening, a parse failure still retries (existing behavior)."""
    compiler = _make_compiler()
    call_count = 0

    async def _bad_json(self_inner, prompt: str, temperature: float = 0.2, **kwargs) -> str:
        nonlocal call_count
        call_count += 1
        # Long enough (> 100 chars) that retry-gating wouldn't skip it if hardening were on.
        return "not json " + "x" * 100

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_parse_hardening = False
        with patch.object(WikiCompiler, "_llm_generate_json", _bad_json):
            result = await compiler._call_llm("test prompt", max_retries=1)

    # With max_retries=1, total attempts = 2.
    assert call_count == 2, f"Expected 2 calls without hardening, got {call_count}"
    assert result.content == ""


@pytest.mark.asyncio
async def test_normal_response_does_not_trigger_gating() -> None:
    """A normal long response with valid JSON is not affected by retry gating."""
    compiler = _make_compiler()
    call_count = 0

    async def _good_response(self_inner, prompt: str, temperature: float = 0.2, **kwargs) -> str:
        nonlocal call_count
        call_count += 1
        return '{"content": "This is a detailed page about the API redesign covering architecture decisions and implementation details spanning multiple paragraphs.", "summary": "API redesign overview."}'

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_parse_hardening = True
        mock_settings.return_value.wiki_compiler_v2 = False
        with patch.object(WikiCompiler, "_llm_generate_json", _good_response):
            result = await compiler._call_llm("test prompt", max_retries=1)

    assert call_count == 1
    assert "API redesign" in result.content


@pytest.mark.asyncio
async def test_blocked_keyword_triggers_no_retry() -> None:
    """Response containing 'BLOCKED' in first 200 chars triggers no-retry."""
    compiler = _make_compiler()
    call_count = 0

    async def _blocked_response(self_inner, prompt: str, temperature: float = 0.2, **kwargs) -> str:
        nonlocal call_count
        call_count += 1
        return "BLOCKED: This content was flagged by our safety filter and cannot be generated."

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        mock_settings.return_value.wiki_parse_hardening = True
        with patch.object(WikiCompiler, "_llm_generate_json", _blocked_response):
            await compiler._call_llm("test prompt", max_retries=1)

    assert call_count == 1
