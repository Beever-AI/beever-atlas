"""Unit tests for QA ADK SSE streaming mode (qa_adk_streaming_sse flag).

Tests the _run_agent_stream generator directly, bypassing HTTP transport,
to verify:
- Flag ON: response_delta fires only on partial=True events, not on final aggregate.
- Flag ON: tool_call_start fires only on non-partial (fully-assembled) function calls.
- Flag ON: accumulated_text equals concatenation of partial text chunks.
- Flag OFF: single response_delta on final event (baseline, byte-identical to pre-change).
"""

from __future__ import annotations

import json
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.api.ask import _run_agent_stream


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_text_event(text: str, *, partial: bool, turn_complete: bool = False) -> MagicMock:
    """Create a mock ADK event carrying regular text content."""
    part = MagicMock()
    part.text = text
    part.thought = False
    content = MagicMock()
    content.parts = [part]
    event = MagicMock()
    event.content = content
    event.partial = partial
    event.turn_complete = turn_complete
    event.error_code = None
    event.error_message = None
    event.get_function_calls.return_value = []
    event.get_function_responses.return_value = []
    return event


def _make_thinking_event(text: str, *, partial: bool) -> MagicMock:
    """Create a mock ADK event with a thought=True part."""
    part = MagicMock()
    part.text = text
    part.thought = True
    content = MagicMock()
    content.parts = [part]
    event = MagicMock()
    event.content = content
    event.partial = partial
    event.turn_complete = False
    event.error_code = None
    event.error_message = None
    event.get_function_calls.return_value = []
    event.get_function_responses.return_value = []
    return event


def _make_function_call_event(name: str, args: dict, *, partial: bool) -> MagicMock:
    """Create a mock ADK event with a function call."""
    fc = MagicMock()
    fc.name = name
    fc.args = args
    event = MagicMock()
    event.content = None
    event.partial = partial
    event.turn_complete = False
    event.error_code = None
    event.error_message = None
    event.get_function_calls.return_value = [fc]
    event.get_function_responses.return_value = []
    return event


def _make_turn_complete_event() -> MagicMock:
    """Create a mock ADK turn_complete event with no text."""
    event = MagicMock()
    event.content = None
    event.partial = False
    event.turn_complete = True
    event.error_code = None
    event.error_message = None
    event.get_function_calls.return_value = []
    event.get_function_responses.return_value = []
    return event


def _parse_sse_events(chunks: list[str]) -> list[tuple[str, dict]]:
    """Parse raw SSE strings into (event_type, data) tuples."""
    events = []
    for chunk in chunks:
        lines = chunk.strip().split("\n")
        event_type = None
        for line in lines:
            if line.startswith("event: "):
                event_type = line[7:].strip()
            elif line.startswith("data: ") and event_type is not None:
                data = json.loads(line[6:].strip())
                events.append((event_type, data))
                event_type = None
    return events


# ---------------------------------------------------------------------------
# Shared test fixture machinery
# ---------------------------------------------------------------------------


def _make_mock_request() -> MagicMock:
    req = MagicMock()
    req.is_disconnected = AsyncMock(return_value=False)
    return req


def _make_mock_session() -> MagicMock:
    session = MagicMock()
    session.user_id = "test_user"
    session.id = "test_session"
    return session


async def _collect_stream(gen: AsyncGenerator[str, None]) -> list[str]:
    chunks = []
    async for chunk in gen:
        chunks.append(chunk)
    return chunks


# ---------------------------------------------------------------------------
# Flag ON: partial text streaming tests
# ---------------------------------------------------------------------------


class TestFlagOnTextStreaming:
    """Flag ON (sse_streaming=True): partial events drive emission, final skipped."""

    @pytest.mark.asyncio
    async def test_three_partials_emit_three_response_deltas(self):
        """3 partial text events → 3 response_delta SSE frames."""
        partial_texts = ["Hello", " world", "!"]
        # Final aggregate carries the full concatenated text
        final_text = "Hello world!"

        async def fake_run_async(**kwargs):
            for t in partial_texts:
                yield _make_text_event(t, partial=True)
            yield _make_text_event(final_text, partial=False)
            yield _make_turn_complete_event()

        session = _make_mock_session()
        runner = MagicMock()
        runner.run_async = fake_run_async

        with (
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()
            ),
            patch("beever_atlas.api.ask.create_runner", return_value=runner),
            patch(
                "beever_atlas.api.ask.create_session", new_callable=AsyncMock, return_value=session
            ),
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt",
                new_callable=AsyncMock,
                return_value=("q", None),
            ),
            patch(
                "beever_atlas.api.ask._load_chat_history_parts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("beever_atlas.api.ask._persist_qa_history", new_callable=AsyncMock),
            patch("beever_atlas.infra.config.get_settings") as mock_settings,
        ):
            settings = MagicMock()
            settings.citation_registry_enabled = False
            settings.qa_adk_streaming_sse = True
            settings.qa_onboarding_length_monitor = False
            mock_settings.return_value = settings

            gen = _run_agent_stream(
                question="q",
                channel_id="C1",
                session_id="S1",
                user_id="U1",
                request=_make_mock_request(),
            )
            chunks = await _collect_stream(gen)

        events = _parse_sse_events(chunks)
        deltas = [(t, d) for t, d in events if t == "response_delta"]

        assert len(deltas) == 3, f"Expected 3 response_deltas, got {len(deltas)}: {deltas}"
        assert deltas[0][1]["delta"] == "Hello"
        assert deltas[1][1]["delta"] == " world"
        assert deltas[2][1]["delta"] == "!"

    @pytest.mark.asyncio
    async def test_no_text_emission_on_final_aggregate(self):
        """Flag ON: final aggregate (partial=False) must not emit a response_delta."""

        async def fake_run_async(**kwargs):
            yield _make_text_event("chunk1", partial=True)
            yield _make_text_event("chunk1", partial=False)  # final — must be suppressed
            yield _make_turn_complete_event()

        session = _make_mock_session()
        runner = MagicMock()
        runner.run_async = fake_run_async

        with (
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()
            ),
            patch("beever_atlas.api.ask.create_runner", return_value=runner),
            patch(
                "beever_atlas.api.ask.create_session", new_callable=AsyncMock, return_value=session
            ),
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt",
                new_callable=AsyncMock,
                return_value=("q", None),
            ),
            patch(
                "beever_atlas.api.ask._load_chat_history_parts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("beever_atlas.api.ask._persist_qa_history", new_callable=AsyncMock),
            patch("beever_atlas.infra.config.get_settings") as mock_settings,
        ):
            settings = MagicMock()
            settings.citation_registry_enabled = False
            settings.qa_adk_streaming_sse = True
            settings.qa_onboarding_length_monitor = False
            mock_settings.return_value = settings

            chunks = await _collect_stream(
                _run_agent_stream(
                    question="q",
                    channel_id="C1",
                    session_id="S1",
                    user_id="U1",
                    request=_make_mock_request(),
                )
            )

        events = _parse_sse_events(chunks)
        deltas = [d for t, d in events if t == "response_delta"]
        # Only the partial=True chunk should have fired
        assert len(deltas) == 1, f"Expected 1 delta, got {len(deltas)}: {deltas}"
        assert deltas[0]["delta"] == "chunk1"

    @pytest.mark.asyncio
    async def test_accumulated_text_equals_partials_only(self):
        """Flag ON: accumulated_text passed to _persist_qa_history equals joined partials."""
        partial_texts = ["foo", " bar"]

        async def fake_run_async(**kwargs):
            for t in partial_texts:
                yield _make_text_event(t, partial=True)
            yield _make_text_event("foo bar (aggregate)", partial=False)
            yield _make_turn_complete_event()

        session = _make_mock_session()
        runner = MagicMock()
        runner.run_async = fake_run_async
        persist_mock = AsyncMock()

        with (
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()
            ),
            patch("beever_atlas.api.ask.create_runner", return_value=runner),
            patch(
                "beever_atlas.api.ask.create_session", new_callable=AsyncMock, return_value=session
            ),
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt",
                new_callable=AsyncMock,
                return_value=("q", None),
            ),
            patch(
                "beever_atlas.api.ask._load_chat_history_parts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("beever_atlas.api.ask._persist_qa_history", persist_mock),
            patch("beever_atlas.infra.config.get_settings") as mock_settings,
        ):
            settings = MagicMock()
            settings.citation_registry_enabled = False
            settings.qa_adk_streaming_sse = True
            settings.qa_onboarding_length_monitor = False
            mock_settings.return_value = settings

            await _collect_stream(
                _run_agent_stream(
                    question="q",
                    channel_id="C1",
                    session_id="S1",
                    user_id="U1",
                    request=_make_mock_request(),
                )
            )

        persist_mock.assert_called_once()
        call_kwargs = persist_mock.call_args.kwargs
        assert call_kwargs["answer"] == "foo bar", (
            f"accumulated_text should be partials only, got: {call_kwargs['answer']!r}"
        )

    @pytest.mark.asyncio
    async def test_thinking_streamed_on_partials_only(self):
        """Flag ON: thinking events fire on partial=True thoughts only."""

        async def fake_run_async(**kwargs):
            yield _make_thinking_event("thought chunk 1", partial=True)
            yield _make_thinking_event("thought chunk 2", partial=True)
            yield _make_thinking_event("thought chunk 1thought chunk 2", partial=False)
            yield _make_turn_complete_event()

        session = _make_mock_session()
        runner = MagicMock()
        runner.run_async = fake_run_async

        with (
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()
            ),
            patch("beever_atlas.api.ask.create_runner", return_value=runner),
            patch(
                "beever_atlas.api.ask.create_session", new_callable=AsyncMock, return_value=session
            ),
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt",
                new_callable=AsyncMock,
                return_value=("q", None),
            ),
            patch(
                "beever_atlas.api.ask._load_chat_history_parts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("beever_atlas.api.ask._persist_qa_history", new_callable=AsyncMock),
            patch("beever_atlas.infra.config.get_settings") as mock_settings,
        ):
            settings = MagicMock()
            settings.citation_registry_enabled = False
            settings.qa_adk_streaming_sse = True
            settings.qa_onboarding_length_monitor = False
            mock_settings.return_value = settings

            chunks = await _collect_stream(
                _run_agent_stream(
                    question="q",
                    channel_id="C1",
                    session_id="S1",
                    user_id="U1",
                    request=_make_mock_request(),
                )
            )

        events = _parse_sse_events(chunks)
        thinking_events = [(t, d) for t, d in events if t == "thinking"]
        assert len(thinking_events) == 2, (
            f"Expected 2 thinking events (partials only), got {len(thinking_events)}"
        )
        assert thinking_events[0][1]["text"] == "thought chunk 1"
        assert thinking_events[1][1]["text"] == "thought chunk 2"


# ---------------------------------------------------------------------------
# Flag ON: tool_call_start only on non-partial events
# ---------------------------------------------------------------------------


class TestFlagOnToolCallGate:
    """Flag ON: tool_call_start fires exactly once (on the complete-args event)."""

    @pytest.mark.asyncio
    async def test_tool_call_start_fires_once_on_complete_args(self):
        """Partial function-call event is suppressed; final fires tool_call_start."""

        async def fake_run_async(**kwargs):
            # Partial: incomplete args — must be suppressed
            yield _make_function_call_event("search_facts", {"query": "inc"}, partial=True)
            # Final: complete args — must fire tool_call_start
            yield _make_function_call_event(
                "search_facts", {"query": "incomplete query"}, partial=False
            )
            yield _make_turn_complete_event()

        session = _make_mock_session()
        runner = MagicMock()
        runner.run_async = fake_run_async

        with (
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()
            ),
            patch("beever_atlas.api.ask.create_runner", return_value=runner),
            patch(
                "beever_atlas.api.ask.create_session", new_callable=AsyncMock, return_value=session
            ),
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt",
                new_callable=AsyncMock,
                return_value=("q", None),
            ),
            patch(
                "beever_atlas.api.ask._load_chat_history_parts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("beever_atlas.api.ask._persist_qa_history", new_callable=AsyncMock),
            patch("beever_atlas.infra.config.get_settings") as mock_settings,
        ):
            settings = MagicMock()
            settings.citation_registry_enabled = False
            settings.qa_adk_streaming_sse = True
            settings.qa_onboarding_length_monitor = False
            mock_settings.return_value = settings

            chunks = await _collect_stream(
                _run_agent_stream(
                    question="q",
                    channel_id="C1",
                    session_id="S1",
                    user_id="U1",
                    request=_make_mock_request(),
                )
            )

        events = _parse_sse_events(chunks)
        starts = [(t, d) for t, d in events if t == "tool_call_start"]
        assert len(starts) == 1, f"Expected exactly 1 tool_call_start, got {len(starts)}: {starts}"
        assert starts[0][1]["tool_name"] == "search_facts"
        assert starts[0][1]["input"] == {"query": "incomplete query"}


# ---------------------------------------------------------------------------
# Flag OFF: baseline byte-identical behavior
# ---------------------------------------------------------------------------


class TestFlagOff:
    """Flag OFF (qa_adk_streaming_sse=False): behavior identical to pre-change."""

    @pytest.mark.asyncio
    async def test_single_response_delta_on_final(self):
        """Flag OFF: final event (partial=False) emits a response_delta normally."""

        async def fake_run_async(**kwargs):
            # With flag off, ADK emits a single non-partial text event
            yield _make_text_event("Full answer here", partial=False)
            yield _make_turn_complete_event()

        session = _make_mock_session()
        runner = MagicMock()
        runner.run_async = fake_run_async

        with (
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()
            ),
            patch("beever_atlas.api.ask.create_runner", return_value=runner),
            patch(
                "beever_atlas.api.ask.create_session", new_callable=AsyncMock, return_value=session
            ),
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt",
                new_callable=AsyncMock,
                return_value=("q", None),
            ),
            patch(
                "beever_atlas.api.ask._load_chat_history_parts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("beever_atlas.api.ask._persist_qa_history", new_callable=AsyncMock),
            patch("beever_atlas.infra.config.get_settings") as mock_settings,
        ):
            settings = MagicMock()
            settings.citation_registry_enabled = False
            settings.qa_adk_streaming_sse = False
            settings.qa_onboarding_length_monitor = False
            mock_settings.return_value = settings

            chunks = await _collect_stream(
                _run_agent_stream(
                    question="q",
                    channel_id="C1",
                    session_id="S1",
                    user_id="U1",
                    request=_make_mock_request(),
                )
            )

        events = _parse_sse_events(chunks)
        deltas = [(t, d) for t, d in events if t == "response_delta"]
        assert len(deltas) == 1, (
            f"Expected exactly 1 response_delta in flag-off mode, got {len(deltas)}"
        )
        assert deltas[0][1]["delta"] == "Full answer here"

    @pytest.mark.asyncio
    async def test_tool_call_start_fires_on_non_partial(self):
        """Flag OFF: tool_call_start fires on the non-partial function call event."""

        async def fake_run_async(**kwargs):
            yield _make_function_call_event("search_facts", {"query": "test"}, partial=False)
            yield _make_turn_complete_event()

        session = _make_mock_session()
        runner = MagicMock()
        runner.run_async = fake_run_async

        with (
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()
            ),
            patch("beever_atlas.api.ask.create_runner", return_value=runner),
            patch(
                "beever_atlas.api.ask.create_session", new_callable=AsyncMock, return_value=session
            ),
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt",
                new_callable=AsyncMock,
                return_value=("q", None),
            ),
            patch(
                "beever_atlas.api.ask._load_chat_history_parts",
                new_callable=AsyncMock,
                return_value=[],
            ),
            patch("beever_atlas.api.ask._persist_qa_history", new_callable=AsyncMock),
            patch("beever_atlas.infra.config.get_settings") as mock_settings,
        ):
            settings = MagicMock()
            settings.citation_registry_enabled = False
            settings.qa_adk_streaming_sse = False
            settings.qa_onboarding_length_monitor = False
            mock_settings.return_value = settings

            chunks = await _collect_stream(
                _run_agent_stream(
                    question="q",
                    channel_id="C1",
                    session_id="S1",
                    user_id="U1",
                    request=_make_mock_request(),
                )
            )

        events = _parse_sse_events(chunks)
        starts = [(t, d) for t, d in events if t == "tool_call_start"]
        assert len(starts) == 1
        assert starts[0][1]["tool_name"] == "search_facts"

    @pytest.mark.asyncio
    async def test_turn_complete_fires_in_both_modes(self):
        """Regardless of flag, turn_complete→done event must always be emitted."""
        for flag in [True, False]:

            async def fake_run_async(**kwargs):
                yield _make_text_event("answer", partial=not flag)
                yield _make_turn_complete_event()

            session = _make_mock_session()
            runner = MagicMock()
            runner.run_async = fake_run_async

            with (
                patch(
                    "beever_atlas.agents.query.qa_agent.get_agent_for_mode",
                    return_value=MagicMock(),
                ),
                patch("beever_atlas.api.ask.create_runner", return_value=runner),
                patch(
                    "beever_atlas.api.ask.create_session",
                    new_callable=AsyncMock,
                    return_value=session,
                ),
                patch(
                    "beever_atlas.api.ask._build_decomposed_prompt",
                    new_callable=AsyncMock,
                    return_value=("q", None),
                ),
                patch(
                    "beever_atlas.api.ask._load_chat_history_parts",
                    new_callable=AsyncMock,
                    return_value=[],
                ),
                patch("beever_atlas.api.ask._persist_qa_history", new_callable=AsyncMock),
                patch("beever_atlas.infra.config.get_settings") as mock_settings,
            ):
                settings = MagicMock()
                settings.citation_registry_enabled = False
                settings.qa_adk_streaming_sse = flag
                settings.qa_onboarding_length_monitor = False
                mock_settings.return_value = settings

                chunks = await _collect_stream(
                    _run_agent_stream(
                        question="q",
                        channel_id="C1",
                        session_id="S1",
                        user_id="U1",
                        request=_make_mock_request(),
                    )
                )

            events = _parse_sse_events(chunks)
            event_types = [t for t, _ in events]
            assert "done" in event_types, f"done event missing with qa_adk_streaming_sse={flag}"
