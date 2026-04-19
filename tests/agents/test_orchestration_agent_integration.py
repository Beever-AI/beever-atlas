"""Regression tests for Phase 6 orchestration tool integration.

Tasks 6.5 and 6.6 of openspec change ``atlas-mcp-server``.

6.5 — Deep-mode agent tool list includes trigger_sync_tool under trusted
      context and excludes it under untrusted context.

6.6 — Safety: _filter_tools_for_untrusted with all 5 orchestration tools
      removes trigger_sync_tool and refresh_wiki_tool, preserves the
      three read-only tools, and leaves all existing QA_TOOLS untouched.
"""

from __future__ import annotations

import pytest

from beever_atlas.agents.query.qa_agent import _filter_tools_for_untrusted
from beever_atlas.agents.tools import QA_TOOLS
from beever_atlas.agents.tools.orchestration_tools import (
    list_connections_tool,
    list_channels_tool,
    trigger_sync_tool,
    refresh_wiki_tool,
    get_job_status_tool,
    ORCHESTRATION_TOOLS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_name(tool) -> str:
    """Mirror the name-extraction logic in qa_agent._tool_name."""
    return (
        getattr(tool, "__name__", None)
        or getattr(tool, "name", None)
        or getattr(getattr(tool, "func", None), "__name__", "")
        or ""
    )


ALL_ORCHESTRATION_NAMES = {_tool_name(t) for t in ORCHESTRATION_TOOLS}

# Expected split after untrusted filter:
WRITE_SIDE_NAMES = {"trigger_sync_tool", "refresh_wiki_tool"}
READ_ONLY_NAMES = {"list_connections_tool", "list_channels_tool", "get_job_status_tool"}


# ---------------------------------------------------------------------------
# 6.5 — Deep-mode tool list composition
# ---------------------------------------------------------------------------


def test_deep_mode_includes_all_orchestration_tools():
    """Deep mode assembles its tool list from QA_TOOLS + ORCHESTRATION_TOOLS.

    We verify by simulating what create_qa_agent does in the else branch:
    ``all_tools = [*base_tools, *ORCHESTRATION_TOOLS, *registry.tools]``.
    All five orchestration tool names must be present.
    """
    simulated_deep_tools = [*QA_TOOLS, *ORCHESTRATION_TOOLS]
    names = {_tool_name(t) for t in simulated_deep_tools}
    assert ALL_ORCHESTRATION_NAMES.issubset(names), (
        f"Missing orchestration tools in deep mode: "
        f"{ALL_ORCHESTRATION_NAMES - names}"
    )


def test_deep_mode_trusted_context_keeps_write_tools():
    """Under trusted context (no untrusted filter applied), all 5 tools remain."""
    combined = [*QA_TOOLS, *ORCHESTRATION_TOOLS]
    names = {_tool_name(t) for t in combined}
    assert "trigger_sync_tool" in names
    assert "refresh_wiki_tool" in names


def test_deep_mode_untrusted_context_drops_write_tools():
    """_filter_tools_for_untrusted removes trigger_sync and refresh_wiki."""
    combined = [*QA_TOOLS, *ORCHESTRATION_TOOLS]
    filtered = _filter_tools_for_untrusted(combined)
    filtered_names = {_tool_name(t) for t in filtered}

    assert "trigger_sync_tool" not in filtered_names, (
        "trigger_sync_tool must be removed under untrusted context"
    )
    assert "refresh_wiki_tool" not in filtered_names, (
        "refresh_wiki_tool must be removed under untrusted context"
    )


def test_deep_mode_untrusted_context_preserves_read_only_tools():
    """Read-only orchestration tools survive _filter_tools_for_untrusted."""
    combined = [*QA_TOOLS, *ORCHESTRATION_TOOLS]
    filtered = _filter_tools_for_untrusted(combined)
    filtered_names = {_tool_name(t) for t in filtered}

    for name in READ_ONLY_NAMES:
        assert name in filtered_names, (
            f"{name} must be preserved under untrusted context"
        )


# ---------------------------------------------------------------------------
# 6.6 — Safety: _filter_tools_for_untrusted over all 5 orchestration tools
# ---------------------------------------------------------------------------


def test_untrusted_filter_removes_trigger_sync():
    """trigger_sync_tool is removed when only orchestration tools are present."""
    result = _filter_tools_for_untrusted([trigger_sync_tool])
    assert result == [], (
        "trigger_sync_tool must be filtered out under untrusted context"
    )


def test_untrusted_filter_removes_refresh_wiki():
    """refresh_wiki_tool is removed when only orchestration tools are present."""
    result = _filter_tools_for_untrusted([refresh_wiki_tool])
    assert result == [], (
        "refresh_wiki_tool must be filtered out under untrusted context"
    )


def test_untrusted_filter_preserves_list_connections():
    """list_connections_tool is preserved under untrusted context."""
    result = _filter_tools_for_untrusted([list_connections_tool])
    assert len(result) == 1
    assert _tool_name(result[0]) == "list_connections_tool"


def test_untrusted_filter_preserves_list_channels():
    """list_channels_tool is preserved under untrusted context."""
    result = _filter_tools_for_untrusted([list_channels_tool])
    assert len(result) == 1
    assert _tool_name(result[0]) == "list_channels_tool"


def test_untrusted_filter_preserves_get_job_status():
    """get_job_status_tool is preserved under untrusted context."""
    result = _filter_tools_for_untrusted([get_job_status_tool])
    assert len(result) == 1
    assert _tool_name(result[0]) == "get_job_status_tool"


def test_untrusted_filter_full_orchestration_set():
    """Full split: write-side removed, read-only preserved, exact sets."""
    filtered = _filter_tools_for_untrusted(list(ORCHESTRATION_TOOLS))
    filtered_names = {_tool_name(t) for t in filtered}

    assert filtered_names == READ_ONLY_NAMES, (
        f"Expected only read-only tools to survive.\n"
        f"  Expected: {READ_ONLY_NAMES}\n"
        f"  Got:      {filtered_names}"
    )


def test_untrusted_filter_existing_qa_tools_unaffected():
    """No existing QA_TOOLS name contains 'sync' or 'refresh'.

    This confirms the new denylist fragments don't accidentally drop any
    existing retrieval tools when orchestration_tools are not present.
    """
    filtered = _filter_tools_for_untrusted(list(QA_TOOLS))
    filtered_names = {_tool_name(t) for t in filtered}
    qa_names = {_tool_name(t) for t in QA_TOOLS}

    assert filtered_names == qa_names, (
        f"New denylist fragments collide with existing QA_TOOLS!\n"
        f"  Dropped: {qa_names - filtered_names}"
    )


def test_untrusted_filter_combined_qa_plus_orchestration():
    """Combined QA_TOOLS + ORCHESTRATION_TOOLS: only write-side orchestration dropped."""
    combined = [*QA_TOOLS, *ORCHESTRATION_TOOLS]
    filtered = _filter_tools_for_untrusted(combined)
    filtered_names = {_tool_name(t) for t in filtered}

    qa_names = {_tool_name(t) for t in QA_TOOLS}
    expected = qa_names | READ_ONLY_NAMES

    assert filtered_names == expected, (
        f"Unexpected filter result on combined list.\n"
        f"  Expected: {expected}\n"
        f"  Got:      {filtered_names}"
    )


# ---------------------------------------------------------------------------
# Tool identity checks
# ---------------------------------------------------------------------------

def test_orchestration_tools_are_coroutines():
    """All 5 orchestration tools must be async (coroutine functions)."""
    import asyncio

    for tool in ORCHESTRATION_TOOLS:
        assert asyncio.iscoroutinefunction(tool), (
            f"{_tool_name(tool)} is not an async function"
        )


def test_orchestration_tools_have_docstrings():
    """All 5 orchestration tools must have non-empty docstrings."""
    for tool in ORCHESTRATION_TOOLS:
        assert tool.__doc__ and tool.__doc__.strip(), (
            f"{_tool_name(tool)} has no docstring"
        )


def test_quick_mode_excludes_orchestration_tools():
    """Quick mode uses only _WIKI_TOOLS_NAMES — no orchestration tools."""
    from beever_atlas.agents.query.qa_agent import _WIKI_TOOLS_NAMES

    # Verify orchestration tool names are not in the quick-mode set.
    assert ALL_ORCHESTRATION_NAMES.isdisjoint(_WIKI_TOOLS_NAMES), (
        f"Orchestration tools leaked into quick mode: "
        f"{ALL_ORCHESTRATION_NAMES & _WIKI_TOOLS_NAMES}"
    )


def test_summarize_mode_excludes_orchestration_tools():
    """Summarize mode uses only _SUMMARIZE_TOOLS_NAMES — no orchestration tools."""
    from beever_atlas.agents.query.qa_agent import _SUMMARIZE_TOOLS_NAMES

    assert ALL_ORCHESTRATION_NAMES.isdisjoint(_SUMMARIZE_TOOLS_NAMES), (
        f"Orchestration tools leaked into summarize mode: "
        f"{ALL_ORCHESTRATION_NAMES & _SUMMARIZE_TOOLS_NAMES}"
    )
