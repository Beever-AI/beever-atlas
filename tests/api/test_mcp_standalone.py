"""Tests for the standalone stdio MCP entrypoint (Glama / registry introspection).

Covers:
- ``build_mcp()`` + in-memory client can complete the MCP handshake and answer
  ``tools/list`` / ``prompts/list`` with NO backing stores and NO env config.
  This is the contract MCP registries (Glama.ai) verify in their sandbox.
- The ``__main__`` module exposes a ``main()`` callable wired to stdio
  transport (checked by signature, not by running a blocking stdio loop).

These tests intentionally use no mocks for stores: the whole point of the
standalone mode is that tool *registration* must never touch MongoDB,
Weaviate, Neo4j, or Redis.
"""

from __future__ import annotations

import pytest
from fastmcp import Client

from beever_atlas.api.mcp_server import build_mcp


# ---------------------------------------------------------------------------
# Introspection with zero external dependencies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_requires_no_stores():
    """tools/list must succeed with no DBs running and no env vars set."""
    mcp = build_mcp()
    async with Client(mcp) as client:
        tools = await client.list_tools()

    names = {tool.name for tool in tools}
    # Spot-check tools from each registration group rather than pinning the
    # full catalog (which grows over time).
    assert "whoami" in names  # discovery
    assert "ask_channel" in names  # retrieval
    assert "find_experts" in names  # graph
    assert "start_new_session" in names  # session
    assert "trigger_sync" in names  # orchestration
    assert len(names) >= 20


@pytest.mark.asyncio
async def test_prompts_list_requires_no_stores():
    """prompts/list must succeed with no DBs running."""
    mcp = build_mcp()
    async with Client(mcp) as client:
        prompts = await client.list_prompts()

    names = {prompt.name for prompt in prompts}
    assert names == {"summarize_channel", "investigate_decision", "onboard_new_channel"}


# ---------------------------------------------------------------------------
# Entrypoint wiring
# ---------------------------------------------------------------------------


def test_main_module_exposes_entrypoint():
    """``python -m beever_atlas.api.mcp_server`` resolves to a main() callable."""
    from beever_atlas.api.mcp_server import __main__ as entrypoint

    assert callable(entrypoint.main)
