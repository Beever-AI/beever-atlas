"""Test that thinking parts never leak into response_delta SSE events."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_part(text: str, thought: bool = False) -> MagicMock:
    part = MagicMock()
    part.text = text
    part.thought = thought
    return part


def _make_event(parts=None, turn_complete=False) -> MagicMock:
    event = MagicMock()
    event.turn_complete = turn_complete
    event.error_code = None
    event.error_message = None
    event.function_calls = []
    event.get_function_calls = MagicMock(return_value=[])
    event.get_function_responses = MagicMock(return_value=[])
    if parts is not None:
        event.content = MagicMock()
        event.content.parts = parts
    else:
        event.content = None
    return event


@pytest.mark.asyncio
async def test_part_thought_not_in_response_delta():
    """A part with thought=True must yield only a 'thinking' event and
    zero 'response_delta' events for that part."""
    from beever_atlas.api.ask import _run_agent_stream

    thought_part = _make_part("internal reasoning text", thought=True)
    plain_part = _make_part("visible answer text", thought=False)

    adk_events = [
        _make_event(parts=[thought_part]),
        _make_event(parts=[plain_part], turn_complete=True),
    ]

    mock_runner = MagicMock()
    mock_session = MagicMock()
    mock_session.id = "sess-test"

    async def fake_run_async(*args, **kwargs):
        for ev in adk_events:
            yield ev

    mock_runner.run_async = fake_run_async

    mock_agent = MagicMock()
    mock_settings = MagicMock()
    mock_settings.citation_registry_enabled = False

    mock_stores = MagicMock()
    mock_stores.qa_history = AsyncMock()
    mock_stores.qa_history.add_turn = AsyncMock()
    mock_stores.chat_history = AsyncMock()
    mock_stores.chat_history.get_recent = AsyncMock(return_value=[])
    mock_stores.chat_history.add_message = AsyncMock()

    mock_request = MagicMock()
    mock_request.is_disconnected = AsyncMock(return_value=False)

    collected_event_types: list[str] = []
    collected_thinking_texts: list[str] = []
    collected_response_deltas: list[str] = []

    with (
        patch("beever_atlas.api.ask.create_runner", return_value=mock_runner),
        patch("beever_atlas.api.ask.create_session", return_value=mock_session),
        patch(
            "beever_atlas.agents.query.qa_agent.get_agent_for_mode",
            return_value=mock_agent,
        ),
        patch(
            "beever_atlas.infra.config.get_settings",
            return_value=mock_settings,
        ),
        patch("beever_atlas.stores.get_stores", return_value=mock_stores),
        patch(
            "beever_atlas.api.ask._load_chat_history_parts",
            new=AsyncMock(return_value=[]),
        ),
        patch(
            "beever_atlas.api.ask._build_decomposed_prompt",
            new=AsyncMock(return_value=("test question", None)),
        ),
        patch(
            "beever_atlas.api.ask._persist_qa_history",
            new=AsyncMock(),
        ),
    ):
        async for chunk in _run_agent_stream(
            question="test question",
            channel_id="C1",
            session_id="sess-test",
            user_id="user-1",
            request=mock_request,
            mode="deep",
            attachments=[],
        ):
            for line in chunk.split("\n"):
                line = line.strip()
                if line.startswith("event:"):
                    collected_event_types.append(line[len("event:") :].strip())
                elif line.startswith("data:"):
                    payload_str = line[len("data:") :].strip()
                    try:
                        payload = json.loads(payload_str)
                    except Exception:
                        continue
                    if collected_event_types:
                        last_event = collected_event_types[-1]
                        if last_event == "thinking":
                            collected_thinking_texts.append(payload.get("text", ""))
                        elif last_event == "response_delta":
                            collected_response_deltas.append(payload.get("delta", ""))

    assert "thinking" in collected_event_types, (
        "Expected a 'thinking' SSE event for the thought part"
    )
    assert any("internal reasoning text" in t for t in collected_thinking_texts), (
        "Thinking text should appear in 'thinking' event payloads"
    )
    for delta in collected_response_deltas:
        assert "internal reasoning text" not in delta, (
            f"Thinking prose leaked into response_delta: {delta!r}"
        )
