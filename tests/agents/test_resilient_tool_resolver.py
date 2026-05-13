"""Tests for the resilient tool resolver.

Validates that hallucinated tool names are intercepted and turned into a
structured tool-result the LLM can recover from — instead of letting
ADK's default ``ValueError`` crash the agent stream.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from google.adk.flows.llm_flows import functions as adk_functions

from beever_atlas.agents.resilient_tool_resolver import (
    _UnknownToolStub,
    install_resilient_tool_resolver,
)


@pytest.fixture(autouse=True)
def _restore_original_get_tool():
    """Each test starts with the un-patched ``_get_tool``; restore after."""
    original = adk_functions._get_tool
    yield
    adk_functions._get_tool = original


def _fake_call(name: str) -> Any:
    fc = MagicMock()
    fc.name = name
    return fc


def _fake_tool(name: str) -> Any:
    """A minimal stand-in for a BaseTool — same identity check as ADK uses."""
    t = MagicMock()
    t.name = name
    return t


def test_install_replaces_get_tool_idempotently():
    """Second install is a no-op — the marker attribute prevents double-wrap."""
    install_resilient_tool_resolver()
    first = adk_functions._get_tool
    assert getattr(first, "_beever_resilient", False) is True

    install_resilient_tool_resolver()
    second = adk_functions._get_tool
    assert second is first, "second install should be idempotent"


def test_known_tool_returned_unchanged():
    """When the LLM names a real tool, the resilient resolver returns it directly."""
    install_resilient_tool_resolver()
    tools = {"search_facts": _fake_tool("search_facts")}

    result = adk_functions._get_tool(_fake_call("search_facts"), tools)

    assert result is tools["search_facts"]


def test_unknown_tool_returns_stub_with_available_names():
    """Hallucinated tool name → ``_UnknownToolStub`` with sorted available names."""
    install_resilient_tool_resolver()
    tools = {
        "search_facts": _fake_tool("search_facts"),
        "find_experts": _fake_tool("find_experts"),
    }

    result = adk_functions._get_tool(_fake_call("people-profile"), tools)

    assert isinstance(result, _UnknownToolStub)
    assert result.name == "people-profile"
    # Sorted so error output is deterministic / matchable in higher-level tests
    assert result._available == ["find_experts", "search_facts"]


@pytest.mark.asyncio
async def test_stub_run_async_returns_structured_error():
    """Stub returns a JSON-serialisable error dict the LLM can react to."""
    stub = _UnknownToolStub("people-profile", ["find_experts", "search_facts"])

    result = await stub.run_async(args={}, tool_context=MagicMock())

    assert result["error"] == "tool_not_found"
    assert result["requested_tool"] == "people-profile"
    assert result["available_tools"] == ["find_experts", "search_facts"]
    assert "case-sensitive" in result["hint"].lower()


def test_unknown_tool_with_no_name_attribute_does_not_crash():
    """Malformed FunctionCall (no ``name`` attr) shouldn't blow up the resolver."""
    install_resilient_tool_resolver()
    nameless = MagicMock(spec=[])  # no ``name`` attribute
    tools = {"x": _fake_tool("x")}

    result = adk_functions._get_tool(nameless, tools)

    assert isinstance(result, _UnknownToolStub)
    assert result.name == "<unknown>"
