"""Tests for Phase 5: delimited response parser + JSON-mode invariants."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from beever_atlas.wiki.compiler import (
    WikiCompiler,
    _parse_delimited_response,
)


# ---------------------------------------------------------------------------
# Parser unit tests
# ---------------------------------------------------------------------------


def test_parse_delimited_happy_path() -> None:
    raw = (
        "###SUMMARY###\n"
        "A concise two-sentence summary. Second sentence here.\n"
        "###CONTENT###\n"
        "# Heading\n\nBody text with [1] citation.\n"
        "###END###\n"
    )
    result = _parse_delimited_response(raw)
    assert "A concise two-sentence summary" in result.summary
    assert result.content.startswith("# Heading")
    assert "Body text" in result.content
    assert "###END###" not in result.content


def test_parse_delimited_missing_end_marker() -> None:
    """No ###END### — content is everything after ###CONTENT###."""
    raw = (
        "###SUMMARY###\n"
        "Summary line.\n"
        "###CONTENT###\n"
        "# Heading\n\nBody text without end marker.\n"
    )
    result = _parse_delimited_response(raw)
    assert result.summary == "Summary line."
    assert "Body text without end marker." in result.content
    assert result.content.startswith("# Heading")


def test_parse_delimited_missing_summary() -> None:
    """No ###SUMMARY### — summary is derived from first sentence of content."""
    raw = (
        "###CONTENT###\n"
        "First sentence of body. Second sentence follows.\n"
        "###END###\n"
    )
    result = _parse_delimited_response(raw)
    assert result.summary.startswith("First sentence of body")
    assert "First sentence of body" in result.content


def test_parse_delimited_preamble_ignored() -> None:
    """LLM prefixes 'Sure, here:' — parser ignores it."""
    raw = (
        "Sure, here:\n"
        "###SUMMARY###\n"
        "The summary.\n"
        "###CONTENT###\n"
        "The real body.\n"
        "###END###\n"
    )
    result = _parse_delimited_response(raw)
    assert result.summary == "The summary."
    assert result.content.strip() == "The real body."
    assert "Sure, here" not in result.content
    assert "Sure, here" not in result.summary


def test_parse_delimited_total_failure_returns_empty() -> None:
    """No ###CONTENT### at all — return empty so retry logic triggers."""
    result = _parse_delimited_response("just some random text with no markers")
    assert result.content == ""
    assert result.summary == ""


def test_marker_echo_survives() -> None:
    """LLM echoes ###CONTENT### in preamble before emitting the real block.

    Per plan line 324: parser uses rsplit semantics — it keeps trailing body
    content rather than preamble when the marker is echoed. So when the LLM
    first apologetically mentions the marker, then emits the real response,
    the real body (after the LAST ###CONTENT###) is returned, not the preamble.
    """
    raw = (
        "Sorry, I will now emit the ###CONTENT### block as requested.\n"
        "###SUMMARY###\n"
        "Actual summary.\n"
        "###CONTENT###\n"
        "The real body text.\n"
        "###END###\n"
    )
    result = _parse_delimited_response(raw)
    assert result.content.strip() == "The real body text."
    assert "Sorry" not in result.content
    assert result.summary == "Actual summary."
    # And a trailing echoed ###END### token inside a fence is tolerated as
    # long as the real ###END### terminates — rpartition strips only the last.
    raw2 = (
        "###SUMMARY###\ns\n"
        "###CONTENT###\n"
        "```\nprotocol uses ###END### as terminator\n```\n"
        "###END###\n"
    )
    result2 = _parse_delimited_response(raw2)
    assert "protocol uses ###END### as terminator" in result2.content
    assert not result2.content.rstrip().endswith("###END###")


# ---------------------------------------------------------------------------
# Integration tests for _llm_generate_json mode selection
# ---------------------------------------------------------------------------


def _make_compiler() -> WikiCompiler:
    provider = MagicMock()
    provider.get_model_string.return_value = "gemini-2.5-flash"
    with patch("beever_atlas.wiki.compiler.get_llm_provider", return_value=provider):
        return WikiCompiler()


@pytest.mark.asyncio
async def test_analysis_always_json_mode() -> None:
    """With wiki_compiler_v2=ON, analysis page_kind MUST still use JSON mode.

    Assert response_mime_type=application/json is set in the Gemini config.
    """
    compiler = _make_compiler()
    captured: dict = {}

    class _FakeResponse:
        text = '{"content": "x", "summary": "y"}'

    class _FakeAioModels:
        async def generate_content(self, model, contents, config):  # noqa: ANN001
            captured["config"] = config
            captured["contents"] = contents
            return _FakeResponse()

    class _FakeAio:
        def __init__(self) -> None:
            self.models = _FakeAioModels()

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = _FakeAio()

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.wiki_compiler_v2 = True  # flag ON
        s.wiki_token_budget_v2 = False
        s.ollama_api_base = "http://localhost:11434"
        with patch("google.genai.Client", return_value=_FakeClient()):
            raw = await compiler._llm_generate_json(
                "prompt body", temperature=0.2, page_kind="analysis"
            )

    assert raw == '{"content": "x", "summary": "y"}'
    # INVARIANT: JSON mode retained for analysis, regardless of flag.
    assert getattr(captured["config"], "response_mime_type", None) == "application/json"
    # Delimited suffix must NOT be appended to analysis prompts.
    assert "###CONTENT###" not in captured["contents"]


@pytest.mark.asyncio
async def test_translation_always_json_mode() -> None:
    """Translation is also JSON-invariant (plan line 310)."""
    compiler = _make_compiler()
    captured: dict = {}

    class _FakeResponse:
        text = '{}'

    class _FakeAioModels:
        async def generate_content(self, model, contents, config):  # noqa: ANN001
            captured["config"] = config
            captured["contents"] = contents
            return _FakeResponse()

    class _FakeAio:
        def __init__(self) -> None:
            self.models = _FakeAioModels()

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = _FakeAio()

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.wiki_compiler_v2 = True
        s.wiki_token_budget_v2 = False
        s.ollama_api_base = "http://localhost:11434"
        with patch("google.genai.Client", return_value=_FakeClient()):
            await compiler._llm_generate_json(
                "prompt body", temperature=0.2, page_kind="translation"
            )

    assert getattr(captured["config"], "response_mime_type", None) == "application/json"
    assert "###CONTENT###" not in captured["contents"]


@pytest.mark.asyncio
async def test_topic_delimited_when_flag_on() -> None:
    """With wiki_compiler_v2=ON and page_kind='topic', delimited mode engages.

    response_mime_type must be unset and the delimited suffix appended.
    """
    compiler = _make_compiler()
    captured: dict = {}

    class _FakeResponse:
        text = "###SUMMARY###\ns\n###CONTENT###\nc\n###END###\n"

    class _FakeAioModels:
        async def generate_content(self, model, contents, config):  # noqa: ANN001
            captured["config"] = config
            captured["contents"] = contents
            return _FakeResponse()

    class _FakeAio:
        def __init__(self) -> None:
            self.models = _FakeAioModels()

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = _FakeAio()

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.wiki_compiler_v2 = True
        s.wiki_token_budget_v2 = False
        s.ollama_api_base = "http://localhost:11434"
        with patch("google.genai.Client", return_value=_FakeClient()):
            await compiler._llm_generate_json(
                "prompt body", temperature=0.2, page_kind="topic"
            )

    assert getattr(captured["config"], "response_mime_type", None) is None
    assert "###CONTENT###" in captured["contents"]
    assert "###SUMMARY###" in captured["contents"]


@pytest.mark.asyncio
async def test_topic_json_mode_when_flag_off() -> None:
    """With wiki_compiler_v2=OFF, topic page uses JSON mode (byte-identical legacy)."""
    compiler = _make_compiler()
    captured: dict = {}

    class _FakeResponse:
        text = '{}'

    class _FakeAioModels:
        async def generate_content(self, model, contents, config):  # noqa: ANN001
            captured["config"] = config
            captured["contents"] = contents
            return _FakeResponse()

    class _FakeAio:
        def __init__(self) -> None:
            self.models = _FakeAioModels()

    class _FakeClient:
        def __init__(self) -> None:
            self.aio = _FakeAio()

    with patch("beever_atlas.infra.config.get_settings") as mock_settings:
        s = mock_settings.return_value
        s.wiki_compiler_v2 = False  # flag OFF
        s.wiki_token_budget_v2 = False
        s.ollama_api_base = "http://localhost:11434"
        with patch("google.genai.Client", return_value=_FakeClient()):
            await compiler._llm_generate_json(
                "prompt body", temperature=0.2, page_kind="topic"
            )

    assert getattr(captured["config"], "response_mime_type", None) == "application/json"
    assert "###CONTENT###" not in captured["contents"]
