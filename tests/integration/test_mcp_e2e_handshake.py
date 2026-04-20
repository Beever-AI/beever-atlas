"""Phase 9 task 9.1 — In-process MCP handshake test.

Full E2E against Claude Code is out of scope without Anthropic infrastructure.
This module exercises the same protocol surface using FastMCP's in-process
``FastMCPTransport`` + ``Client``, which speaks the real MCP JSON-RPC wire
protocol through in-memory streams — functionally equivalent to a real client
from the server's perspective.

What is tested:
    1. ``tools/list``      → 16 v1 tools returned
    2. ``resources/list``  → 5 URI templates returned
    3. ``prompts/list``    → 3 prompts returned
    4. ``whoami`` tool     → principal_id injection works (ASGI scope.state mock)
    5. ``get_wiki_page``   → channel_access_denied error for unowned channel

Auth note:
    ``MCPAuthMiddleware`` runs at the ASGI HTTP layer, which the in-memory
    transport bypasses (it connects directly to the MCP protocol layer).
    Principal injection is therefore done by mocking
    ``fastmcp.server.dependencies.get_http_request`` to return a fake request
    whose ``scope["state"]`` contains ``mcp_principal_id``.  This mirrors the
    exact same technique used in ``test_mcp_job_cross_surface.py``.

Rate-limit note:
    ``mcp_rate_limit.reset_state()`` is called before each test to prevent
    cross-test counter bleed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Expected catalog counts (must match Phase 3–5b registrations)
# ---------------------------------------------------------------------------

_EXPECTED_TOOL_COUNT = 16
_EXPECTED_RESOURCE_COUNT = 5
_EXPECTED_PROMPT_COUNT = 3

_PRINCIPAL_ID = "mcp:test-principal-abc123"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_fake_request(principal_id: str = _PRINCIPAL_ID) -> MagicMock:
    """Build a minimal fake ASGI request whose scope.state carries the principal."""
    req = MagicMock()
    req.scope = {"state": {"mcp_principal_id": principal_id}}
    return req


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Ensure rate-limit counters do not bleed between tests."""
    from beever_atlas.infra import mcp_rate_limit

    mcp_rate_limit.reset_state()
    yield
    mcp_rate_limit.reset_state()


@pytest.fixture
def mcp_instance():
    """Build a fresh FastMCP instance for each test."""
    from beever_atlas.api.mcp_server import build_mcp

    return build_mcp()


# ---------------------------------------------------------------------------
# 9.1.1  tools/list — 16 tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_returns_all_16_tools(mcp_instance):
    """Client tools/list must enumerate all 16 registered v1 tools.

    Tool names expected (16):
        Shim (1): search_channel_knowledge
        Discovery (3): whoami, list_connections, list_channels
        Retrieval (5): ask_channel, search_channel_facts, get_wiki_page,
                       get_recent_activity, search_media_references
        Graph (3): find_experts, search_relationships, trace_decision_history
        Session (1): start_new_session
        Orchestration (3): trigger_sync, refresh_wiki, get_job_status
    """
    from fastmcp.client import Client, FastMCPTransport

    transport = FastMCPTransport(mcp_instance)
    async with Client(transport) as client:
        tools = await client.list_tools()

    tool_names = {t.name for t in tools}
    assert len(tools) == _EXPECTED_TOOL_COUNT, (
        f"Expected {_EXPECTED_TOOL_COUNT} tools, got {len(tools)}: {sorted(tool_names)}"
    )

    # Spot-check key tool names across all groups
    expected_names = {
        # shim
        "search_channel_knowledge",
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
        # orchestration
        "trigger_sync",
        "refresh_wiki",
        "get_job_status",
    }
    missing = expected_names - tool_names
    assert not missing, f"Missing tools: {missing}"


# ---------------------------------------------------------------------------
# 9.1.2  resources/list — 5 URI templates
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resources_list_returns_5_uri_templates(mcp_instance):
    """resources/templates/list must enumerate the 5 atlas:// URI templates.

    All atlas:// resources are parameterised (contain ``{...}`` placeholders),
    so they appear under ``resources/listResourceTemplates``, not
    ``resources/list``. The Client exposes this as ``list_resource_templates()``.

    URIs expected (5):
        atlas://connection/{connection_id}
        atlas://connection/{connection_id}/channels
        atlas://channel/{channel_id}/wiki
        atlas://channel/{channel_id}/wiki/page/{page_id}
        atlas://job/{job_id}
    """
    from fastmcp.client import Client, FastMCPTransport

    transport = FastMCPTransport(mcp_instance)
    async with Client(transport) as client:
        resources = await client.list_resource_templates()

    assert len(resources) == _EXPECTED_RESOURCE_COUNT, (
        f"Expected {_EXPECTED_RESOURCE_COUNT} resource templates, got {len(resources)}: "
        f"{[r.uriTemplate if hasattr(r, 'uriTemplate') else str(r) for r in resources]}"
    )


# ---------------------------------------------------------------------------
# 9.1.3  prompts/list — 3 prompts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompts_list_returns_3_prompts(mcp_instance):
    """Client prompts/list must enumerate the 3 registered prompt templates.

    Prompts expected (3):
        summarize_channel, investigate_decision, onboard_new_channel
    """
    from fastmcp.client import Client, FastMCPTransport

    transport = FastMCPTransport(mcp_instance)
    async with Client(transport) as client:
        prompts = await client.list_prompts()

    prompt_names = {p.name for p in prompts}
    assert len(prompts) == _EXPECTED_PROMPT_COUNT, (
        f"Expected {_EXPECTED_PROMPT_COUNT} prompts, got {len(prompts)}: {sorted(prompt_names)}"
    )
    expected_prompts = {"summarize_channel", "investigate_decision", "onboard_new_channel"}
    missing = expected_prompts - prompt_names
    assert not missing, f"Missing prompts: {missing}"


# ---------------------------------------------------------------------------
# 9.1.4  whoami — principal_id injection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_whoami_returns_injected_principal_id(mcp_instance):
    """whoami must return the principal_id from the mocked ASGI scope.state.

    The tool calls ``_get_principal_id(ctx)`` which reads from
    ``fastmcp.server.dependencies.get_http_request().scope["state"]``.
    We mock that function to return a fake request carrying our test principal.
    """
    fake_request = _make_fake_request(_PRINCIPAL_ID)

    # Mock stores so list_connections doesn't hit MongoDB
    stores_mock = MagicMock()
    stores_mock.platform.list_connections = AsyncMock(return_value=[])

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=fake_request,
        ),
        patch(
            "beever_atlas.stores.get_stores",
            return_value=stores_mock,
        ),
    ):
        # Call the tool function directly via the internal registry to avoid
        # the FastMCPTransport's in-memory stream path, which doesn't propagate
        # our mock into the server's execution context.
        fn = _get_tool_fn(mcp_instance, "whoami")
        assert fn is not None, "whoami tool not found in registry"

        ctx_mock = MagicMock()
        result = await fn(ctx=ctx_mock)

    assert result.get("principal_id") == _PRINCIPAL_ID, (
        f"Expected principal_id={_PRINCIPAL_ID!r}, got {result!r}"
    )
    assert "connections" in result
    assert "server_version" in result


# ---------------------------------------------------------------------------
# 9.1.5  get_wiki_page — channel_access_denied for unowned channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_wiki_page_unowned_channel_returns_access_denied(mcp_instance):
    """get_wiki_page with an unowned channel must return channel_access_denied.

    The tool calls assert_channel_access() which raises HTTPException(403)
    when no matching connection is found for an MCP principal in multi-tenant
    mode.  The tool translates this to {error: "channel_access_denied"}.
    """
    fake_request = _make_fake_request(_PRINCIPAL_ID)

    # No connections → assert_channel_access raises 403 for MCP principals
    stores_mock = MagicMock()
    stores_mock.platform.list_connections = AsyncMock(return_value=[])

    with (
        patch(
            "fastmcp.server.dependencies.get_http_request",
            return_value=fake_request,
        ),
        patch(
            "beever_atlas.stores.get_stores",
            return_value=stores_mock,
        ),
    ):
        fn = _get_tool_fn(mcp_instance, "get_wiki_page")
        assert fn is not None, "get_wiki_page tool not found in registry"

        ctx_mock = MagicMock()
        result = await fn(channel_id="ch-x", page_type="overview", ctx=ctx_mock)

    assert result.get("error") == "channel_access_denied", (
        f"Expected channel_access_denied, got: {result!r}"
    )
    assert result.get("channel_id") == "ch-x"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_tool_fn(mcp, tool_name: str):
    """Extract the wrapped tool function from the FastMCP component registry."""
    for key, component in mcp._local_provider._components.items():
        if not key.startswith("tool:"):
            continue
        raw = key[len("tool:") :]
        name = raw.split("@")[0]
        if name == tool_name and hasattr(component, "fn"):
            return component.fn
    return None
