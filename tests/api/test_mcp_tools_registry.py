"""Phase 3 task 3.8/3.9: verify the tool registry has exactly the right set.

`tools/list` (via build_mcp()) must return exactly 14 entries:
  - 13 v1 tools (3 discovery + 5 retrieval + 3 graph + 1 session + 1 shim)

Note: orchestration tools (trigger_sync, refresh_wiki, get_job_status) are
Phase 5b and MUST NOT appear in the registry.
"""

from __future__ import annotations

import pytest

from beever_atlas.api.mcp_server import build_mcp

# The spec says 15 tools total for v1, but orchestration (trigger_sync,
# refresh_wiki, get_job_status) are Phase 5b.  Phase 3 ships 14:
#   whoami, list_connections, list_channels            (discovery  ×3)
#   ask_channel, search_channel_facts, get_wiki_page,
#   get_recent_activity, search_media_references       (retrieval  ×5)
#   find_experts, search_relationships,
#   trace_decision_history                             (graph      ×3)
#   start_new_session                                  (session    ×1)
#   search_channel_knowledge                           (shim       ×1)

EXPECTED_TOOLS = frozenset({
    "whoami",
    "list_connections",
    "list_channels",
    "ask_channel",
    "search_channel_facts",
    "get_wiki_page",
    "get_recent_activity",
    "search_media_references",
    "find_experts",
    "search_relationships",
    "trace_decision_history",
    "start_new_session",
    "search_channel_knowledge",
})

# Orchestration tools are deferred to Phase 5b — they must NOT appear yet.
DEFERRED_TOOLS = frozenset({"trigger_sync", "refresh_wiki", "get_job_status"})


def _tool_names(mcp) -> frozenset[str]:
    """Extract registered tool names from the FastMCP instance.

    FastMCP 3.x stores components in ``mcp._local_provider._components`` with
    keys like ``"tool:whoami@"`` (name + optional version suffix after ``@``).
    We strip the ``"tool:"`` prefix and the ``@<version>`` suffix.
    """
    result = set()
    for k in mcp._local_provider._components:
        if k.startswith("tool:"):
            # Key is "tool:<name>@<version>" or "tool:<name>@" — strip prefix and version
            name_version = k[len("tool:"):]
            name = name_version.split("@")[0]
            result.add(name)
    return frozenset(result)


def test_tool_registry_contains_expected_set():
    mcp = build_mcp()
    names = _tool_names(mcp)
    assert names == EXPECTED_TOOLS, (
        f"Tool registry mismatch.\n"
        f"  Extra:   {names - EXPECTED_TOOLS}\n"
        f"  Missing: {EXPECTED_TOOLS - names}"
    )


def test_tool_count_is_thirteen():
    mcp = build_mcp()
    names = _tool_names(mcp)
    assert len(names) == len(EXPECTED_TOOLS), (
        f"Expected {len(EXPECTED_TOOLS)} tools, got {len(names)}: {sorted(names)}"
    )


def test_orchestration_tools_not_registered():
    mcp = build_mcp()
    names = _tool_names(mcp)
    leaked = DEFERRED_TOOLS & names
    assert not leaked, (
        f"Phase 5b orchestration tools must not be registered yet: {leaked}"
    )


def test_all_tools_have_non_empty_description():
    mcp = build_mcp()
    for component_key, tool in mcp._local_provider._components.items():
        if not component_key.startswith("tool:"):
            continue
        name = component_key[len("tool:"):].split("@")[0]
        desc = getattr(tool, "description", None) or ""
        assert desc.strip(), (
            f"Tool '{name}' has an empty or missing description. "
            "Every tool must have an LLM-oriented description per task 3.9."
        )


def test_build_mcp_is_idempotent():
    """Calling build_mcp() twice should produce independent instances with the same tool set."""
    mcp1 = build_mcp()
    mcp2 = build_mcp()
    assert _tool_names(mcp1) == _tool_names(mcp2)
