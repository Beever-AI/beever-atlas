"""Tests for the deep-mode empty-retrieval recovery behaviour.

These tests pin two contracts:

1. The deep-mode system prompt includes a section that tells the agent
   to suggest syncing / building a wiki when retrieval comes back empty,
   with an explicit "no auto-trigger" guardrail.

2. The `suggest_follow_ups` tool is pass-through — whatever the model
   passes as chip strings reaches the UI collector unchanged (aside
   from whitespace / bullet-prefix trimming), so the model-authored
   "Sync this channel now" chip is emitted verbatim.

A full deep-mode end-to-end test would require running a real Gemini /
ADK runner; we keep these tests deterministic and fast by exercising
the prompt-assembly and follow-ups tool surfaces directly. The prompt
guardrail on "no auto-trigger" is the enforceable line of defence
against unwanted side effects on a diagnostic question.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Prompt-assembly contract
# ---------------------------------------------------------------------------


def _make_settings(*, registry_on: bool = True, new_prompt: bool = True) -> MagicMock:
    s = MagicMock()
    s.citation_registry_enabled = registry_on
    s.qa_new_prompt = new_prompt
    return s


@pytest.mark.parametrize("new_prompt", [False, True])
def test_deep_prompt_contains_empty_retrieval_recovery(new_prompt: bool):
    """Both prompt paths (new + legacy) must embed the recovery section in deep mode."""
    settings = _make_settings(new_prompt=new_prompt)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=True, mode="deep")
    assert "Empty-retrieval recovery" in prompt


@pytest.mark.parametrize("new_prompt", [False, True])
def test_deep_prompt_mentions_sync_suggestion(new_prompt: bool):
    """The prompt must instruct the agent to mention 'sync' when channel is un-synced."""
    settings = _make_settings(new_prompt=new_prompt)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=True, mode="deep")
    lower = prompt.lower()
    assert "sync" in lower
    assert "un-synced" in lower or "hasn't been synced" in lower


@pytest.mark.parametrize("new_prompt", [False, True])
def test_deep_prompt_forbids_auto_trigger(new_prompt: bool):
    """The prompt must explicitly forbid auto-calling trigger_sync_tool / refresh_wiki_tool."""
    settings = _make_settings(new_prompt=new_prompt)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=True, mode="deep")
    assert "trigger_sync_tool" in prompt
    assert "refresh_wiki_tool" in prompt
    # The guardrail: "Do NOT call ... automatically" (or equivalent)
    assert "DO NOT call" in prompt or "Do NOT call" in prompt
    assert "explicit user consent" in prompt or "explicitly asked" in prompt


@pytest.mark.parametrize("new_prompt", [False, True])
def test_deep_prompt_asks_for_action_chip(new_prompt: bool):
    """Prompt must tell the model to include an action-oriented follow-up chip."""
    settings = _make_settings(new_prompt=new_prompt)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=True, mode="deep")
    # Example chips the prompt specifies verbatim
    assert "Sync this channel now" in prompt
    assert "Build a wiki for this channel" in prompt


def test_quick_mode_does_not_include_recovery_section():
    """Recovery section is deep-mode-only; quick mode must NOT include it."""
    settings = _make_settings(new_prompt=True)
    with patch("beever_atlas.infra.config.get_settings", return_value=settings):
        from beever_atlas.agents.query.prompts import build_qa_system_prompt

        prompt = build_qa_system_prompt(max_tool_calls=8, include_follow_ups=True, mode="quick")
    assert "Empty-retrieval recovery" not in prompt


# ---------------------------------------------------------------------------
# Follow-ups tool pass-through contract
# ---------------------------------------------------------------------------


def test_follow_ups_tool_passes_sync_chip_through():
    """Model-authored 'Sync this channel now' must reach the collector verbatim."""
    from beever_atlas.agents.query.follow_ups_tool import (
        bind_collector,
        reset_collector,
        suggest_follow_ups,
    )

    c, tok = bind_collector()
    try:
        result = suggest_follow_ups(
            [
                "Sync this channel now",
                "Build a wiki for this channel",
                "Try searching for specific keywords",
            ]
        )
    finally:
        reset_collector(tok)

    assert result == {"ok": True, "count": 3}
    assert c.questions == [
        "Sync this channel now",
        "Build a wiki for this channel",
        "Try searching for specific keywords",
    ]


def test_follow_ups_tool_preserves_sync_substring_after_bullet_strip():
    """Even if the model prefixes a bullet or number, 'sync' must survive stripping."""
    from beever_atlas.agents.query.follow_ups_tool import (
        bind_collector,
        reset_collector,
        suggest_follow_ups,
    )

    c, tok = bind_collector()
    try:
        suggest_follow_ups(
            [
                "- Sync this channel now",
                "1. Build a wiki for this channel",
            ]
        )
    finally:
        reset_collector(tok)

    assert any("sync" in q.lower() for q in c.questions)
    assert any("wiki" in q.lower() for q in c.questions)


# ---------------------------------------------------------------------------
# No-auto-trigger guardrail (defence in depth: capabilities layer)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orchestration_tools_not_awaited_without_principal():
    """Sanity: the orchestration tools require a bound principal — calling them
    bare (no context, no consent) returns a structured error instead of firing
    a capability. This documents the second line of defence: even if the model
    ignored the prompt guardrail, the tool would refuse without a principal.
    """
    from beever_atlas.agents.tools.orchestration_tools import (
        refresh_wiki_tool,
        trigger_sync_tool,
    )

    # Patch the underlying capabilities so a mis-fire would be observable.
    trigger_spy = AsyncMock()
    refresh_spy = AsyncMock()

    with (
        patch("beever_atlas.capabilities.sync.trigger_sync", new=trigger_spy),
        patch("beever_atlas.capabilities.wiki.refresh_wiki", new=refresh_spy),
    ):
        sync_result = await trigger_sync_tool(channel_id="C123")
        wiki_result = await refresh_wiki_tool(channel_id="C123")

    # With no principal bound, both tools short-circuit with an error.
    assert sync_result.get("error") == "no_principal"
    assert wiki_result.get("error") == "no_principal"
    # And the underlying capabilities were never invoked.
    trigger_spy.assert_not_awaited()
    refresh_spy.assert_not_awaited()
