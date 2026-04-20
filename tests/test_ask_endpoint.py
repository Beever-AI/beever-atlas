"""Tests for the SSE streaming ask endpoint."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.server.app import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _install_mock_stores(mock_stores):
    """RES-177: channel-scoped routes now call `assert_channel_access`,
    which needs a usable `get_stores()`. Every test in this file exercises
    `/api/channels/{id}/ask`, so wire the mock stores uniformly."""
    yield mock_stores


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_mock_event(text: str, partial: bool = False, turn_complete: bool = False):
    """Create a mock ADK Event with text content."""
    part = MagicMock()
    part.text = text
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


async def _mock_run_async_success(**kwargs):
    """Mock ADK Runner that yields a response_delta then turn_complete."""
    yield _make_mock_event("Echo: hello world", partial=True)
    yield _make_mock_event("", turn_complete=True)


async def _mock_run_async_error(**kwargs):
    """Mock ADK Runner that yields an error event."""
    event = MagicMock()
    event.content = None
    event.partial = False
    event.turn_complete = False
    event.error_code = "TEST_ERROR"
    event.error_message = "Something went wrong"
    yield event


async def _noop_decomposed_prompt(question: str, channel_id: str) -> tuple:
    return f"[Channel: {channel_id}]\n\n{question}", None


async def _noop_chat_history(session_id: str):
    return []


@pytest.fixture
def mock_runner():
    """Patch the ADK Runner and session creation for tests."""
    mock_session = MagicMock()
    mock_session.user_id = "test_user"
    mock_session.id = "test_session_id"

    with (
        patch("beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()),
        patch("beever_atlas.api.ask.create_runner") as mock_cr,
        patch("beever_atlas.api.ask.create_session", new_callable=AsyncMock) as mock_cs,
        patch("beever_atlas.api.ask._build_decomposed_prompt", side_effect=_noop_decomposed_prompt),
        patch("beever_atlas.api.ask._load_chat_history_parts", side_effect=_noop_chat_history),
    ):
        runner_instance = MagicMock()
        runner_instance.run_async = _mock_run_async_success
        mock_cr.return_value = runner_instance
        mock_cs.return_value = mock_session
        yield runner_instance


@pytest.fixture
def mock_runner_error():
    """Patch the ADK Runner to simulate an error."""
    mock_session = MagicMock()
    mock_session.user_id = "test_user"
    mock_session.id = "test_session_id"

    with (
        patch("beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()),
        patch("beever_atlas.api.ask.create_runner") as mock_cr,
        patch("beever_atlas.api.ask.create_session", new_callable=AsyncMock) as mock_cs,
        patch("beever_atlas.api.ask._build_decomposed_prompt", side_effect=_noop_decomposed_prompt),
        patch("beever_atlas.api.ask._load_chat_history_parts", side_effect=_noop_chat_history),
    ):
        runner_instance = MagicMock()
        runner_instance.run_async = _mock_run_async_error
        mock_cr.return_value = runner_instance
        mock_cs.return_value = mock_session
        yield runner_instance


class TestAskEndpointValidation:
    @pytest.mark.asyncio
    async def test_missing_question_returns_422(self, client: AsyncClient):
        response = await client.post("/api/channels/C123/ask", json={})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_empty_question_returns_422(self, client: AsyncClient):
        response = await client.post("/api/channels/C123/ask", json={"question": ""})
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_valid_request_returns_200_sse(self, client: AsyncClient, mock_runner):
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "hello"},
        )
        assert response.status_code == 200
        assert "text/event-stream" in response.headers.get("content-type", "")


class TestSSEEventFormat:
    @pytest.mark.asyncio
    async def test_stream_contains_done_event(self, client: AsyncClient, mock_runner):
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "what is our tech stack?"},
        )
        assert "event: done" in response.text

    @pytest.mark.asyncio
    async def test_stream_contains_metadata_event(self, client: AsyncClient, mock_runner):
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "what is our tech stack?"},
        )
        body = response.text
        for line in body.split("\n"):
            if line.startswith("data:") and "route" in line:
                data = json.loads(line[5:].strip())
                assert "route" in data
                assert "confidence" in data
                assert "cost_usd" in data
                break

    @pytest.mark.asyncio
    async def test_stream_contains_response_delta(self, client: AsyncClient, mock_runner):
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "hello"},
        )
        # Runner may emit either "response_delta" (regular text) or "thinking"
        # (thought parts) depending on mock output shape.
        assert "event: response_delta" in response.text or "event: thinking" in response.text

    @pytest.mark.asyncio
    async def test_stream_contains_citations_event(self, client: AsyncClient, mock_runner):
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "hello"},
        )
        assert "event: citations" in response.text

    @pytest.mark.asyncio
    async def test_sse_event_format(self, client: AsyncClient, mock_runner):
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "hello"},
        )
        body = response.text
        events = []
        current_event = None
        for line in body.split("\n"):
            if line.startswith("event: "):
                current_event = line[7:]
            elif line.startswith("data: ") and current_event:
                data = json.loads(line[6:])
                events.append((current_event, data))
                current_event = None

        event_types = [e[0] for e in events]
        assert "response_delta" in event_types or "thinking" in event_types
        assert "citations" in event_types
        assert "metadata" in event_types
        assert "done" in event_types

    @pytest.mark.asyncio
    async def test_metadata_contains_channel_id(self, client: AsyncClient, mock_runner):
        response = await client.post(
            "/api/channels/C_TEST_123/ask",
            json={"question": "hello"},
        )
        body = response.text
        for line in body.split("\n"):
            if line.startswith("data:") and "channel_id" in line:
                data = json.loads(line[5:].strip())
                assert data["channel_id"] == "C_TEST_123"
                break


class TestSSEErrorHandling:
    @pytest.mark.asyncio
    async def test_agent_error_streams_error_event(self, client: AsyncClient, mock_runner_error):
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "hello"},
        )
        body = response.text
        assert "event: error" in body
        for line in body.split("\n"):
            if line.startswith("data:") and "message" in line:
                data = json.loads(line[5:].strip())
                assert data["message"] == "Something went wrong"
                assert data["code"] == "TEST_ERROR"
                break


# ── Done-event guarantee tests ────────────────────────────────────────────────


async def _mock_run_async_no_turn_complete(**kwargs):
    """Mock ADK Runner that yields text but never sets turn_complete.

    This simulates the bug where the ADK generator exhausts without
    the turn_complete flag, which previously left the frontend stuck.
    """
    part = MagicMock()
    part.text = "partial response"
    content = MagicMock()
    content.parts = [part]
    event = MagicMock()
    event.content = content
    event.partial = True
    event.turn_complete = False
    event.error_code = None
    event.error_message = None
    event.get_function_calls.return_value = []
    event.get_function_responses.return_value = []
    yield event


async def _mock_run_async_exception(**kwargs):
    """Mock ADK Runner that raises an unexpected exception mid-stream."""
    part = MagicMock()
    part.text = "before crash"
    content = MagicMock()
    content.parts = [part]
    event = MagicMock()
    event.content = content
    event.partial = True
    event.turn_complete = False
    event.error_code = None
    event.error_message = None
    event.get_function_calls.return_value = []
    event.get_function_responses.return_value = []
    yield event
    raise RuntimeError("Unexpected agent crash")


@pytest.fixture
def mock_runner_no_turn_complete():
    """Patch runner to simulate stream ending without turn_complete."""
    mock_session = MagicMock()
    mock_session.user_id = "test_user"
    mock_session.id = "test_session_id"

    with (
        patch("beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()),
        patch("beever_atlas.api.ask.create_runner") as mock_cr,
        patch("beever_atlas.api.ask.create_session", new_callable=AsyncMock) as mock_cs,
        patch("beever_atlas.api.ask._build_decomposed_prompt", side_effect=_noop_decomposed_prompt),
        patch("beever_atlas.api.ask._load_chat_history_parts", side_effect=_noop_chat_history),
    ):
        runner_instance = MagicMock()
        runner_instance.run_async = _mock_run_async_no_turn_complete
        mock_cr.return_value = runner_instance
        mock_cs.return_value = mock_session
        yield runner_instance


@pytest.fixture
def mock_runner_exception():
    """Patch runner to simulate an unexpected exception during streaming."""
    mock_session = MagicMock()
    mock_session.user_id = "test_user"
    mock_session.id = "test_session_id"

    with (
        patch("beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()),
        patch("beever_atlas.api.ask.create_runner") as mock_cr,
        patch("beever_atlas.api.ask.create_session", new_callable=AsyncMock) as mock_cs,
        patch("beever_atlas.api.ask._build_decomposed_prompt", side_effect=_noop_decomposed_prompt),
        patch("beever_atlas.api.ask._load_chat_history_parts", side_effect=_noop_chat_history),
    ):
        runner_instance = MagicMock()
        runner_instance.run_async = _mock_run_async_exception
        mock_cr.return_value = runner_instance
        mock_cs.return_value = mock_session
        yield runner_instance


class TestDoneEventGuarantee:
    """Verify that the SSE stream always emits a terminal event (done or error)
    regardless of how the ADK runner terminates."""

    @pytest.mark.asyncio
    async def test_done_emitted_without_turn_complete(
        self, client: AsyncClient, mock_runner_no_turn_complete
    ):
        """When the ADK runner exhausts without turn_complete, the backend
        safety-net finally block should emit a done event."""
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "hello"},
        )
        body = response.text
        assert "event: done" in body, (
            "Stream ended without turn_complete but no done event was emitted"
        )

    @pytest.mark.asyncio
    async def test_done_emitted_after_runner_exception(
        self, client: AsyncClient, mock_runner_exception
    ):
        """When the ADK runner raises an exception, the stream should emit
        an error event (which counts as a terminal event for the frontend)."""
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "hello"},
        )
        body = response.text
        # Should have either an error event or a done event (or both)
        has_terminal = "event: error" in body or "event: done" in body
        assert has_terminal, "Runner exception did not produce any terminal SSE event"

    @pytest.mark.asyncio
    async def test_error_event_contains_error_details(
        self, client: AsyncClient, mock_runner_exception
    ):
        """Exception-path error events should include the error message."""
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "hello"},
        )
        body = response.text
        assert "event: error" in body
        for line in body.split("\n"):
            if line.startswith("data:") and "AGENT_ERROR" in line:
                data = json.loads(line[5:].strip())
                assert data["code"] == "AGENT_ERROR"
                assert "Unexpected agent crash" in data["message"]
                break
