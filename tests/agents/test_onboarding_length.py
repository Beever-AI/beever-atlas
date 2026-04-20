"""Tests for the onboarding response length monitor (warn-only)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_settings(monitor_on: bool = True):
    s = MagicMock()
    s.qa_onboarding_length_monitor = monitor_on
    s.citation_registry_enabled = False
    return s


async def _drain(gen):
    """Consume an async generator, return list of emitted SSE strings."""
    events = []
    async for chunk in gen:
        events.append(chunk)
    return events


def _make_turn_complete_event(text: str):
    """Build a minimal ADK-style event that yields text and signals turn_complete."""
    part = MagicMock()
    part.text = text
    part.thought = False

    content = MagicMock()
    content.parts = [part]
    content.role = "model"

    event = MagicMock()
    event.content = content
    event.turn_complete = True
    # These must be falsy so the error-branch is NOT triggered
    event.error_code = None
    event.error_message = None
    event.get_function_calls = MagicMock(return_value=[])
    event.get_function_responses = MagicMock(return_value=[])
    return event


async def _async_events(*events):
    for e in events:
        yield e


# ---------------------------------------------------------------------------
# Base patch context manager factory
# ---------------------------------------------------------------------------


def _base_patches(settings, fake_runner, fake_session):
    """Return a list of patch objects that stub out all heavy dependencies."""
    return [
        patch("beever_atlas.api.ask.create_runner", return_value=fake_runner),
        patch("beever_atlas.api.ask.create_session", new=AsyncMock(return_value=fake_session)),
        patch("beever_atlas.api.ask._load_chat_history_parts", new=AsyncMock(return_value=[])),
        patch(
            "beever_atlas.api.ask._build_decomposed_prompt",
            new=AsyncMock(return_value=("test question", None)),
        ),
        patch("beever_atlas.api.ask._persist_qa_history", new=AsyncMock()),
        patch("beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()),
        patch("beever_atlas.infra.config.get_settings", return_value=settings),
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_over_cap_logs_warning():
    """Non-deep response > 1500 chars must trigger logger.warning."""
    settings = _make_mock_settings(monitor_on=True)

    fake_session = MagicMock()
    fake_session.id = "sess-test"
    fake_session.user_id = "u1"

    fake_runner = MagicMock()
    long_text = "A" * 1600
    event = _make_turn_complete_event(long_text)
    fake_runner.run_async = MagicMock(return_value=_async_events(event))

    fake_request = MagicMock()
    fake_request.is_disconnected = AsyncMock(return_value=False)

    import beever_atlas.api.ask as ask_mod
    from beever_atlas.api.ask import _run_agent_stream

    patches = _base_patches(settings, fake_runner, fake_session)
    with patch.object(ask_mod.logger, "warning") as mock_warn:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            await _drain(
                _run_agent_stream(
                    question="what is this channel about?",
                    channel_id="C123",
                    session_id="sess-test",
                    user_id="u1",
                    request=fake_request,
                    mode="quick",
                )
            )

    calls_text = [str(c) for c in mock_warn.call_args_list]
    assert any("exceeded 1500" in t for t in calls_text), (
        f"Expected 'exceeded 1500' warning, got calls: {calls_text}"
    )


@pytest.mark.asyncio
async def test_under_cap_silent():
    """Non-deep response <= 1500 chars must NOT trigger the length warning."""
    settings = _make_mock_settings(monitor_on=True)

    fake_session = MagicMock()
    fake_session.id = "sess-test"
    fake_session.user_id = "u1"

    fake_runner = MagicMock()
    short_text = "A" * 100
    event = _make_turn_complete_event(short_text)
    fake_runner.run_async = MagicMock(return_value=_async_events(event))

    fake_request = MagicMock()
    fake_request.is_disconnected = AsyncMock(return_value=False)

    import beever_atlas.api.ask as ask_mod
    from beever_atlas.api.ask import _run_agent_stream

    patches = _base_patches(settings, fake_runner, fake_session)
    with patch.object(ask_mod.logger, "warning") as mock_warn:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            await _drain(
                _run_agent_stream(
                    question="hi there",
                    channel_id="C123",
                    session_id="sess-test",
                    user_id="u1",
                    request=fake_request,
                    mode="quick",
                )
            )

    length_calls = [c for c in mock_warn.call_args_list if "exceeded 1500" in str(c)]
    assert length_calls == [], f"Unexpected length warning: {length_calls}"


@pytest.mark.asyncio
async def test_deep_mode_no_warn():
    """Even a very long response in deep mode must NOT trigger the length warning."""
    settings = _make_mock_settings(monitor_on=True)

    fake_session = MagicMock()
    fake_session.id = "sess-test"
    fake_session.user_id = "u1"

    fake_runner = MagicMock()
    long_text = "A" * 3000
    event = _make_turn_complete_event(long_text)
    fake_runner.run_async = MagicMock(return_value=_async_events(event))

    fake_request = MagicMock()
    fake_request.is_disconnected = AsyncMock(return_value=False)

    import beever_atlas.api.ask as ask_mod
    from beever_atlas.api.ask import _run_agent_stream

    patches = _base_patches(settings, fake_runner, fake_session)
    with patch.object(ask_mod.logger, "warning") as mock_warn:
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6]:
            await _drain(
                _run_agent_stream(
                    question="explain everything in detail",
                    channel_id="C123",
                    session_id="sess-test",
                    user_id="u1",
                    request=fake_request,
                    mode="deep",
                )
            )

    length_calls = [c for c in mock_warn.call_args_list if "exceeded 1500" in str(c)]
    assert length_calls == [], f"Unexpected length warning in deep mode: {length_calls}"
