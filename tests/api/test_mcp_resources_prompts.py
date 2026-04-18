"""Phase 4 tasks 4.1–4.9: integration tests for MCP resources and prompts.

Tests:
- resources/list returns all 5 URI templates
- prompts/list returns the 3 prompt names
- Resource read denied path (ChannelAccessDenied → structured error)
- Prompt invocation: summarize_channel with channel_id + since_days
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.api.mcp_server import build_mcp


# ---------------------------------------------------------------------------
# Helpers (mirrors pattern in test_mcp_tools_discovery.py)
# ---------------------------------------------------------------------------


def _resource_uris(mcp) -> frozenset[str]:
    """Return all resource URI templates (and static URIs) registered on *mcp*.

    FastMCP 3.x stores resources under key prefix ``"resource:"`` and
    resource templates under ``"template:"`` in ``_local_provider._components``.
    Template keys look like ``"template:atlas://channel/{channel_id}/wiki@"``.
    """
    uris: set[str] = set()
    for key in mcp._local_provider._components:
        if key.startswith("resource:") or key.startswith("template:"):
            # Strip prefix and trailing "@<version>" suffix.
            raw = key.split(":", 1)[1]  # e.g. "atlas://job/{job_id}@"
            uri = raw.rsplit("@", 1)[0]  # strip version suffix
            uris.add(uri)
    return frozenset(uris)


def _prompt_names(mcp) -> frozenset[str]:
    """Return all prompt names registered on *mcp*.

    Keys look like ``"prompt:summarize_channel@"``.
    """
    names: set[str] = set()
    for key in mcp._local_provider._components:
        if key.startswith("prompt:"):
            raw = key[len("prompt:"):]  # e.g. "summarize_channel@"
            name = raw.rsplit("@", 1)[0]
            names.add(name)
    return frozenset(names)


def _get_resource_fn(mcp, uri_fragment: str):
    """Return the underlying async function for a resource whose URI contains *uri_fragment*."""
    for key, component in mcp._local_provider._components.items():
        if (key.startswith("resource:") or key.startswith("template:")) and uri_fragment in key:
            return component.fn
    raise KeyError(f"No resource with URI fragment '{uri_fragment}' found in registry")


def _get_prompt_fn(mcp, name: str):
    """Return the underlying function for a prompt by name."""
    for key, component in mcp._local_provider._components.items():
        if key.startswith(f"prompt:{name}@") or key == f"prompt:{name}":
            return component.fn
    raise KeyError(f"No prompt named '{name}' found in registry")


def _patch_principal(principal_id: str | None = "mcp:testhash"):
    """Patch get_http_request so _get_principal_id_from_resource returns the given principal."""
    request_mock = MagicMock()
    request_mock.scope = {"state": {"mcp_principal_id": principal_id}}
    return patch(
        "fastmcp.server.dependencies.get_http_request",
        return_value=request_mock,
    )


# ---------------------------------------------------------------------------
# 4.9a: resources/list declares all 5 URI templates
# ---------------------------------------------------------------------------

EXPECTED_RESOURCE_URIS = frozenset({
    "atlas://connection/{connection_id}",
    "atlas://connection/{connection_id}/channels",
    "atlas://channel/{channel_id}/wiki",
    "atlas://channel/{channel_id}/wiki/page/{page_id}",
    "atlas://job/{job_id}",
})


def test_resources_list_returns_all_five_uris():
    mcp = build_mcp()
    uris = _resource_uris(mcp)
    assert uris == EXPECTED_RESOURCE_URIS, (
        f"Resource URI mismatch.\n"
        f"  Extra:   {uris - EXPECTED_RESOURCE_URIS}\n"
        f"  Missing: {EXPECTED_RESOURCE_URIS - uris}"
    )


# ---------------------------------------------------------------------------
# 4.9b: prompts/list returns the 3 prompt names
# ---------------------------------------------------------------------------

EXPECTED_PROMPT_NAMES = frozenset({
    "summarize_channel",
    "investigate_decision",
    "onboard_new_channel",
})


def test_prompts_list_returns_three_names():
    mcp = build_mcp()
    names = _prompt_names(mcp)
    assert names == EXPECTED_PROMPT_NAMES, (
        f"Prompt name mismatch.\n"
        f"  Extra:   {names - EXPECTED_PROMPT_NAMES}\n"
        f"  Missing: {EXPECTED_PROMPT_NAMES - names}"
    )


# ---------------------------------------------------------------------------
# 4.9c: tool count stays at 13 (resources/prompts don't count against cap)
# ---------------------------------------------------------------------------


def test_tool_count_still_thirteen_after_resources_and_prompts():
    """Adding resources + prompts must not change the tool count (stays at 13)."""
    from tests.api.test_mcp_tools_registry import _tool_names, EXPECTED_TOOLS

    mcp = build_mcp()
    names = _tool_names(mcp)
    assert names == EXPECTED_TOOLS, (
        f"Tool registry changed after adding resources/prompts.\n"
        f"  Extra:   {names - EXPECTED_TOOLS}\n"
        f"  Missing: {EXPECTED_TOOLS - names}"
    )


# ---------------------------------------------------------------------------
# 4.9d: resource read denied path — ChannelAccessDenied → structured error
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wiki_index_resource_returns_structured_error_on_access_denied():
    """When assert_channel_access raises ChannelAccessDenied the resource returns a structured error."""
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    mcp = build_mcp()
    fn = _get_resource_fn(mcp, "atlas://channel/{channel_id}/wiki")

    with _patch_principal("mcp:testhash"), patch(
        "beever_atlas.capabilities.wiki.get_topic_overview",
        new=AsyncMock(side_effect=ChannelAccessDenied("ch-denied")),
    ):
        result = await fn(channel_id="ch-denied")

    assert result.get("error") == "channel_access_denied"
    assert result.get("channel_id") == "ch-denied"


@pytest.mark.asyncio
async def test_wiki_page_resource_returns_structured_error_on_access_denied():
    """atlas://channel/{id}/wiki/page/{page_id} returns channel_access_denied on denial."""
    from beever_atlas.capabilities.errors import ChannelAccessDenied

    mcp = build_mcp()
    fn = _get_resource_fn(mcp, "atlas://channel/{channel_id}/wiki/page/{page_id}")

    with _patch_principal("mcp:testhash"), patch(
        "beever_atlas.capabilities.wiki.get_wiki_page",
        new=AsyncMock(side_effect=ChannelAccessDenied("ch-denied")),
    ):
        result = await fn(channel_id="ch-denied", page_id="overview")

    assert result.get("error") == "channel_access_denied"


# ---------------------------------------------------------------------------
# 4.9e: prompt invocation — summarize_channel content and parameter threading
# ---------------------------------------------------------------------------


def test_summarize_channel_prompt_contains_channel_and_days():
    """summarize_channel(channel_id='ch-a', since_days=14) produces message referencing both."""
    mcp = build_mcp()
    fn = _get_prompt_fn(mcp, "summarize_channel")
    messages = fn(channel_id="ch-a", since_days=14)

    assert isinstance(messages, list)
    assert len(messages) >= 1
    msg = messages[0]
    assert msg.get("role") == "user"
    content = msg.get("content", "")
    assert "#ch-a" in content, f"Expected '#ch-a' in prompt content: {content!r}"
    assert "14" in content, f"Expected '14' (days) in prompt content: {content!r}"


def test_summarize_channel_default_since_days():
    """summarize_channel with only channel_id uses the default 7-day window."""
    mcp = build_mcp()
    fn = _get_prompt_fn(mcp, "summarize_channel")
    messages = fn(channel_id="ch-b")

    content = messages[0].get("content", "")
    assert "7" in content


def test_investigate_decision_prompt_contains_channel_and_topic():
    """investigate_decision produces a message naming the channel and topic."""
    mcp = build_mcp()
    fn = _get_prompt_fn(mcp, "investigate_decision")
    messages = fn(channel_id="ch-eng", topic="database choice")

    content = messages[0].get("content", "")
    assert "ch-eng" in content
    assert "database choice" in content


def test_onboard_new_channel_prompt_contains_channel():
    """onboard_new_channel produces a message naming the channel."""
    mcp = build_mcp()
    fn = _get_prompt_fn(mcp, "onboard_new_channel")
    messages = fn(channel_id="ch-new")

    content = messages[0].get("content", "")
    assert "ch-new" in content


# ---------------------------------------------------------------------------
# 4.9f: job resource returns job_not_found on JobNotFound
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_job_resource_returns_job_not_found():
    """atlas://job/{job_id} returns job_not_found when capability raises JobNotFound."""
    from beever_atlas.capabilities.errors import JobNotFound

    mcp = build_mcp()
    fn = _get_resource_fn(mcp, "atlas://job/{job_id}")

    with _patch_principal("mcp:testhash"), patch(
        "beever_atlas.capabilities.jobs.get_job_status",
        new=AsyncMock(side_effect=JobNotFound("job-xyz")),
    ):
        result = await fn(job_id="job-xyz")

    assert result.get("error") == "job_not_found"
    assert result.get("job_id") == "job-xyz"


# ---------------------------------------------------------------------------
# 4.9g: connection resource returns connection_access_denied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connection_resource_returns_not_found_for_unowned():
    """atlas://connection/{id} returns connection_not_found when the connection isn't visible."""
    mcp = build_mcp()
    fn = _get_resource_fn(mcp, "atlas://connection/{connection_id}")

    with _patch_principal("mcp:testhash"), patch(
        "beever_atlas.capabilities.connections.list_connections",
        new=AsyncMock(return_value=[]),  # no connections visible → not found
    ):
        result = await fn(connection_id="conn-other")

    assert result.get("error") == "connection_not_found"
