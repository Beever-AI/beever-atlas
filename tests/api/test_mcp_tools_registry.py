"""Phase 3/5b task 3.8/3.9/5.2-5.4: verify the tool registry has exactly the right set.

`tools/list` (via build_mcp()) must return exactly 17 entries:
  - 13 v1 tools (3 discovery + 5 retrieval + 3 graph + 1 session + 1 shim)
  - 3 orchestration tools added in Phase 5b (trigger_sync, refresh_wiki, get_job_status)

Total: 17 (within the 18-tool v1 cap).
"""

from __future__ import annotations

from beever_atlas.api.mcp_server import build_mcp

# Phase 5b adds 3 orchestration tools on top of the 13 v1 + 1 shim = 14.
# New total: 17.
#   whoami, list_connections, list_channels            (discovery      ×3)
#   ask_channel, search_channel_facts, get_wiki_page,
#   get_recent_activity, search_media_references       (retrieval      ×5)
#   find_experts, search_relationships,
#   trace_decision_history                             (graph          ×3)
#   start_new_session                                  (session        ×1)
#   search_channel_knowledge                           (shim           ×1)
#   trigger_sync, refresh_wiki, get_job_status         (orchestration  ×3)

EXPECTED_TOOLS = frozenset(
    {
        # discovery
        "whoami",
        "list_connections",
        "list_channels",
        # retrieval
        "ask_channel",
        "search_channel_facts",
        "get_wiki_page",
        "get_recent_activity",
        "search_media_references",
        # graph
        "find_experts",
        "search_relationships",
        "trace_decision_history",
        # session
        "start_new_session",
        # shim
        "search_channel_knowledge",
        # orchestration (Phase 5b)
        "trigger_sync",
        "refresh_wiki",
        "get_job_status",
    }
)

# No orchestration tools are deferred any longer — all shipped in Phase 5b.
DEFERRED_TOOLS: frozenset[str] = frozenset()


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
            name_version = k[len("tool:") :]
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


def test_tool_count_is_seventeen():
    mcp = build_mcp()
    names = _tool_names(mcp)
    assert len(names) == len(EXPECTED_TOOLS), (
        f"Expected {len(EXPECTED_TOOLS)} tools, got {len(names)}: {sorted(names)}"
    )


def test_orchestration_tools_are_registered():
    """Phase 5b: trigger_sync, refresh_wiki, get_job_status must now be present."""
    orchestration = frozenset({"trigger_sync", "refresh_wiki", "get_job_status"})
    mcp = build_mcp()
    names = _tool_names(mcp)
    missing = orchestration - names
    assert not missing, f"Phase 5b orchestration tools must be registered: {missing}"


def test_all_tools_have_non_empty_description():
    mcp = build_mcp()
    for component_key, tool in mcp._local_provider._components.items():
        if not component_key.startswith("tool:"):
            continue
        name = component_key[len("tool:") :].split("@")[0]
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
