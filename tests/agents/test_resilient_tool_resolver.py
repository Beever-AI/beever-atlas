"""Tests for the resilient tool resolver.

Validates that hallucinated tool names are intercepted and turned into a
structured tool-result the LLM can recover from — instead of letting
ADK's default ``ValueError`` crash the agent stream.

ADK 2.1.0 routes the unknown-tool ``ValueError`` through an agent-level
``on_tool_error_callback``. These tests cover the callback in isolation
plus an integration-style test that drives ADK's real flow function
``_execute_single_function_call_async`` with a ``FunctionCall`` for a
nonexistent tool.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from google.adk.agents import LlmAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.flows.llm_flows import functions as adk_functions
from google.adk.sessions import InMemorySessionService
from google.adk.tools.base_tool import BaseTool
from google.genai import types

from beever_atlas.agents.resilient_tool_resolver import (
    _closest_tool_match,
    build_unknown_tool_payload,
    make_tool_error_callback,
)


def _placeholder_tool(name: str) -> BaseTool:
    """Mirror ADK's placeholder: ``BaseTool(name=..., description='Tool not found')``."""
    return BaseTool(name=name, description="Tool not found")


# ---------------------------------------------------------------------------
# build_unknown_tool_payload — the shared payload builder
# ---------------------------------------------------------------------------


def test_payload_no_close_match_falls_back_to_generic_hint():
    """No close match → JSON-serialisable error dict with the case-sensitive hint."""
    payload = build_unknown_tool_payload("people-profile", ["find_experts", "search_facts"])

    assert payload["error"] == "tool_not_found"
    assert payload["requested_tool"] == "people-profile"
    assert payload["available_tools"] == ["find_experts", "search_facts"]
    assert "case-sensitive" in payload["hint"].lower()
    assert "did_you_mean" not in payload


def test_payload_suggests_closest_when_suffix_dropped():
    """User-reported case: gemma4:e2b called 'list_channels' instead of
    the real 'list_channels_tool'. The payload should auto-suggest the
    suffix so a weak model can recover in one retry."""
    payload = build_unknown_tool_payload(
        "list_channels",
        ["list_channels_tool", "list_skills", "search_qa_history"],
    )

    assert payload["did_you_mean"] == "list_channels_tool"
    assert "list_channels_tool" in payload["hint"]


# ---------------------------------------------------------------------------
# _closest_tool_match — the suggestion logic
# ---------------------------------------------------------------------------


def test_closest_match_suffix_drop():
    assert (
        _closest_tool_match("list_channels", ["list_channels_tool", "search_qa_history"])
        == "list_channels_tool"
    )


def test_closest_match_typo_via_difflib():
    """Generic typo fallback via difflib — swap/missing-char drift."""
    assert (
        _closest_tool_match("sercch_facts", ["search_facts", "search_qa_history", "load_skill"])
        == "search_facts"
    )


def test_closest_match_dash_vs_underscore():
    """Dash-vs-underscore drift — common with GLM-style models."""
    assert (
        _closest_tool_match("find-experts", ["find_experts", "search_qa_history"]) == "find_experts"
    )


def test_closest_match_returns_none_when_unrelated():
    assert _closest_tool_match("totally_made_up", ["find_experts", "search_facts"]) is None


# ---------------------------------------------------------------------------
# make_tool_error_callback — the agent-level callback
# ---------------------------------------------------------------------------


def test_callback_returns_payload_for_unknown_tool():
    """A 'not found' ValueError on the placeholder tool yields the did_you_mean payload."""
    callback = make_tool_error_callback(["find_experts", "search_facts"])
    error = ValueError("Tool 'find-experts' not found.")

    result = callback(
        _placeholder_tool("find-experts"),
        {},
        MagicMock(),
        error,
    )

    assert result["error"] == "tool_not_found"
    assert result["requested_tool"] == "find-experts"
    assert result["did_you_mean"] == "find_experts"


def test_callback_passes_through_non_not_found_errors():
    """A real tool failure (not the unknown-tool ValueError) returns None so
    ADK re-raises it — the fail-fast contract is preserved."""
    callback = make_tool_error_callback(["search_facts"])

    assert (
        callback(_placeholder_tool("search_facts"), {}, MagicMock(), RuntimeError("boom")) is None
    )
    # A ValueError without 'not found' is a genuine tool error, not a name miss.
    assert (
        callback(_placeholder_tool("search_facts"), {}, MagicMock(), ValueError("bad arg")) is None
    )


def test_callback_accepts_callable_name_source():
    """``available_tool_names`` may be a callable resolved lazily at call time."""
    names: list[str] = []
    callback = make_tool_error_callback(lambda: names)
    names.extend(["search_facts", "find_experts"])

    result = callback(
        _placeholder_tool("searc_facts"),
        {},
        MagicMock(),
        ValueError("Tool 'searc_facts' not found."),
    )

    assert result["available_tools"] == ["find_experts", "search_facts"]
    assert result["did_you_mean"] == "search_facts"


# ---------------------------------------------------------------------------
# Integration — drive ADK's real flow function end-to-end
# ---------------------------------------------------------------------------


async def _make_invocation_context(agent: LlmAgent) -> InvocationContext:
    svc = InMemorySessionService()
    session = await svc.create_session(app_name="test_app", user_id="u")
    return InvocationContext(
        session_service=svc,
        invocation_id="iv-test",
        agent=agent,
        session=session,
    )


@pytest.mark.asyncio
async def test_adk_flow_injects_payload_for_unknown_tool():
    """End-to-end: ADK's ``_execute_single_function_call_async`` runs the
    agent-level callback for a hallucinated tool name and injects the
    did_you_mean payload as the function_response (no ValueError escapes)."""
    agent = LlmAgent(
        name="qa_probe_agent",
        model="gemini-2.0-flash",
        instruction="x",
        on_tool_error_callback=make_tool_error_callback(
            ["search_facts", "find_experts", "list_channels_tool"]
        ),
    )
    ic = await _make_invocation_context(agent)
    fc = types.FunctionCall(name="people-profile", args={})

    event = await adk_functions._execute_single_function_call_async(ic, fc, {}, agent)

    assert event is not None
    function_response = event.content.parts[0].function_response
    assert function_response.name == "people-profile"
    assert function_response.response["error"] == "tool_not_found"
    assert function_response.response["requested_tool"] == "people-profile"
    assert function_response.response["available_tools"] == [
        "find_experts",
        "list_channels_tool",
        "search_facts",
    ]


@pytest.mark.asyncio
async def test_adk_flow_without_callback_still_raises():
    """Backward-compatible: a tool-bearing agent WITHOUT the callback keeps
    ADK's hard-fail contract (the original ValueError propagates)."""
    agent = LlmAgent(name="bare_agent", model="gemini-2.0-flash", instruction="x")
    ic = await _make_invocation_context(agent)
    fc = types.FunctionCall(name="people-profile", args={})

    with pytest.raises(ValueError, match="not found"):
        await adk_functions._execute_single_function_call_async(ic, fc, {}, agent)
