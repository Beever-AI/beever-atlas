"""Integration tests for QueryDecomposer (Fix 6).

Tests mock the LLM provider — no network calls.

Scenarios:
1. Multi-aspect question → is_simple=False, >=2 internal queries.
2. Simple question      → is_simple=True, 1 internal query (fast path).
3. Ollama/non-string model fallback → is_simple=False, 1 internal query
   (degraded but UI still shows decomposition event).
4. Timeout fallback → is_simple=False, 1 internal query (degraded).
5. JSON parse failure → is_simple=False, 1 internal query (degraded).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.agents.query.decomposer import decompose, _is_simple


# ---------------------------------------------------------------------------
# _is_simple unit tests
# ---------------------------------------------------------------------------


def test_is_simple_short_single_topic():
    assert _is_simple("what is beever") is True


def test_is_simple_greeting():
    assert _is_simple("hello") is True


def test_is_simple_over_10_words():
    # 11-word question should be complex (>10 words)
    assert (
        _is_simple("tell me everything about the architecture of this project right now") is False
    )


def test_is_simple_with_and():
    assert _is_simple("economic and tech impact") is False


def test_is_simple_with_or():
    assert _is_simple("quick or slow deployment") is False


def test_is_simple_with_comma():
    assert _is_simple("compare X, Y, Z") is False


def test_is_simple_multiple_question_marks():
    assert _is_simple("who built this? and why?") is False


def test_is_simple_vs_keyword():
    assert _is_simple("old vs new approach") is False


# ---------------------------------------------------------------------------
# Multi-aspect question → is_simple=False, >=2 internal queries
# ---------------------------------------------------------------------------


def _install_fake_genai(mock_response):
    """Patch `google.genai.Client` so `client.aio.models.generate_content`
    returns `mock_response` without touching the network.

    Patches both the ``google.genai`` attribute on the ``google`` package AND
    ``sys.modules["google.genai"]`` — ``from google import genai`` resolves
    via attribute access once the submodule has been imported anywhere in
    the test session, so sys.modules alone is insufficient.
    """
    fake_client = MagicMock()
    fake_client.aio.models.generate_content = AsyncMock(return_value=mock_response)
    fake_genai_module = MagicMock()
    fake_genai_module.Client = MagicMock(return_value=fake_client)

    class _Combined:
        def __enter__(self):
            self._sysmod = patch.dict("sys.modules", {"google.genai": fake_genai_module})
            self._attr = patch("google.genai", fake_genai_module, create=True)
            self._sysmod.start()
            self._attr.start()
            return fake_genai_module

        def __exit__(self, *exc):
            self._attr.stop()
            self._sysmod.stop()

    return _Combined()


@pytest.mark.asyncio
async def test_multi_aspect_decomposes_to_multiple_queries():
    """A multi-aspect question should produce is_simple=False and >=2 internal queries."""
    llm_response_json = json.dumps(
        {
            "internal_queries": [
                {"query": "economic impact of beever atlas", "focus": "economic"},
                {"query": "tech impact of beever atlas", "focus": "tech"},
            ],
            "external_queries": [],
        }
    )

    mock_response = MagicMock()
    mock_response.text = llm_response_json

    mock_provider = MagicMock()
    mock_provider.resolve_model = MagicMock(return_value="gemini-1.5-flash-lite")

    with (
        patch(
            "beever_atlas.llm.provider.get_llm_provider",
            return_value=mock_provider,
        ),
        _install_fake_genai(mock_response),
    ):
        plan = await decompose("what is the economic and tech impact of beever atlas")

    assert plan.is_simple is False, "Multi-aspect question must not be simple"
    assert len(plan.internal_queries) >= 2, (
        f"Expected >=2 internal queries, got {len(plan.internal_queries)}"
    )
    focuses = {sq.focus for sq in plan.internal_queries}
    assert "economic" in focuses or "tech" in focuses, (
        f"Expected economic/tech focus labels, got {focuses}"
    )


# ---------------------------------------------------------------------------
# Simple question → is_simple=True, fast-path (no LLM call)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_simple_question_fast_path():
    """Short single-topic questions should skip decomposition entirely."""
    mock_provider = MagicMock()
    mock_provider.resolve_model = MagicMock(return_value="gemini-1.5-flash-lite")

    with patch(
        "beever_atlas.llm.provider.get_llm_provider",
        return_value=mock_provider,
    ) as mock_get_provider:
        plan = await decompose("what is this channel")

    assert plan.is_simple is True
    assert len(plan.internal_queries) == 1
    assert plan.internal_queries[0].focus == "main"
    # Provider should NOT have been called at all
    mock_get_provider.assert_not_called()


# ---------------------------------------------------------------------------
# Ollama/non-string model fallback → is_simple=False, 1 query (degraded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ollama_fallback_returns_is_simple_false():
    """When resolve_model returns a non-string (Ollama), plan should be
    is_simple=False with 1 internal query so the UI shows a degraded event."""
    mock_provider = MagicMock()
    # Simulate Ollama: resolve_model returns a non-string object
    mock_provider.resolve_model = MagicMock(return_value=MagicMock())  # non-str

    with patch(
        "beever_atlas.llm.provider.get_llm_provider",
        return_value=mock_provider,
    ):
        plan = await decompose("what is the economic and tech impact of beever atlas")

    assert plan.is_simple is False, (
        "Ollama fallback must return is_simple=False so UI shows degraded event"
    )
    assert len(plan.internal_queries) == 1
    assert plan.internal_queries[0].query == "what is the economic and tech impact of beever atlas"


# ---------------------------------------------------------------------------
# Timeout fallback → is_simple=False, 1 query (degraded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_fallback_returns_is_simple_false():
    """A timeout during LLM call must yield is_simple=False with 1 query."""

    mock_provider = MagicMock()
    mock_provider.resolve_model = MagicMock(return_value="gemini-1.5-flash-lite")

    async def _timeout_wait_for(coro, timeout):
        # Close the coroutine so it doesn't trigger "never awaited" warnings,
        # then raise as the real wait_for would on timeout.
        coro.close()
        raise asyncio.TimeoutError()

    mock_response = MagicMock()

    with (
        patch(
            "beever_atlas.llm.provider.get_llm_provider",
            return_value=mock_provider,
        ),
        _install_fake_genai(mock_response),
        patch(
            "beever_atlas.agents.query.decomposer.asyncio.wait_for",
            side_effect=_timeout_wait_for,
        ),
    ):
        plan = await decompose("what is the economic and tech impact of beever atlas")

    assert plan.is_simple is False
    assert len(plan.internal_queries) == 1


# ---------------------------------------------------------------------------
# JSON parse failure → is_simple=False, 1 query (degraded)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_parse_failure_returns_is_simple_false():
    """A malformed LLM JSON response must yield is_simple=False with 1 query."""
    mock_response = MagicMock()
    mock_response.text = "NOT VALID JSON {{{"

    mock_provider = MagicMock()
    mock_provider.resolve_model = MagicMock(return_value="gemini-1.5-flash-lite")

    with (
        patch(
            "beever_atlas.llm.provider.get_llm_provider",
            return_value=mock_provider,
        ),
        _install_fake_genai(mock_response),
    ):
        plan = await decompose("what is the economic and tech impact of beever atlas")

    assert plan.is_simple is False
    assert len(plan.internal_queries) == 1
