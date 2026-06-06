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


async def _noop_chat_history(session_id: str, *args, **kwargs):
    # Accepts the ACL context (user_id, channel_id) the ask runner now passes.
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


class TestPrincipalMemoryIdentitySplit:
    """Regression: tool ACL binds the authenticated principal; conversation
    memory keys on the bridge-asserted platform user_id. The two must not be
    conflated (else orchestration tools see an empty connection list)."""

    @pytest.mark.asyncio
    async def test_principal_for_tools_platform_id_for_memory(self, client: AsyncClient):
        captured: dict = {}

        async def _spy_history(session_id, user_id=None, channel_id=None, *a, **k):
            captured["history_user_id"] = user_id
            captured["history_channel_id"] = channel_id
            return []

        def _spy_bind_principal(pid):
            captured["bound_principal"] = pid
            return None

        mock_session = MagicMock()
        mock_session.user_id = "U_PLATFORM"
        mock_session.id = "sess"

        with (
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode", return_value=MagicMock()
            ),
            patch("beever_atlas.api.ask.create_runner") as mock_cr,
            patch("beever_atlas.api.ask.create_session", new_callable=AsyncMock) as mock_cs,
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt", side_effect=_noop_decomposed_prompt
            ),
            patch("beever_atlas.api.ask._load_chat_history_parts", side_effect=_spy_history),
            patch(
                "beever_atlas.agents.tools.orchestration_tools.bind_principal",
                side_effect=_spy_bind_principal,
            ),
        ):
            runner_instance = MagicMock()
            runner_instance.run_async = _mock_run_async_success
            mock_cr.return_value = runner_instance
            mock_cs.return_value = mock_session
            resp = await client.post(
                "/api/channels/C123/ask",
                json={"question": "hi", "user_id": "U_PLATFORM"},
            )
            assert resp.status_code == 200
            _ = resp.text  # drain the SSE stream so the generator runs

        # Tool ACL must NOT be the platform id (it is the authenticated principal).
        assert captured.get("bound_principal") not in (None, "U_PLATFORM")
        # Conversation memory keys on the platform id + channel.
        assert captured.get("history_user_id") == "U_PLATFORM"
        assert captured.get("history_channel_id") == "C123"


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


class _FakeRegistry:
    """Minimal stand-in for SourceRegistry for confidence-scoring tests."""

    def __init__(self, registered: int, referenced: int, scores: list[float]):
        self._registered = registered
        self._referenced = referenced
        self._scores = scores

    @property
    def registered_count(self) -> int:
        return self._registered

    @property
    def referenced_count(self) -> int:
        return self._referenced

    def retrieval_scores(self) -> list[float]:
        return self._scores


class TestComputeConfidence:
    """Honest confidence replaces the old hardcoded 0.85."""

    def test_none_or_empty_registry_is_low(self):
        from beever_atlas.api.ask import _compute_confidence

        assert _compute_confidence(None) == 0.15
        assert _compute_confidence(_FakeRegistry(0, 0, [])) == 0.15

    def test_rich_retrieval_is_high_but_computed(self):
        from beever_atlas.api.ask import _compute_confidence

        val = _compute_confidence(_FakeRegistry(6, 6, [0.9, 0.9, 0.8]))
        assert 0.85 <= val <= 0.95

    def test_thin_retrieval_is_low_enough_to_warn(self):
        from beever_atlas.api.ask import _compute_confidence

        # one source, not cited inline, mediocre score
        assert _compute_confidence(_FakeRegistry(1, 0, [0.4])) <= 0.35

    def test_increases_with_breadth(self):
        from beever_atlas.api.ask import _compute_confidence

        few = _compute_confidence(_FakeRegistry(1, 1, [0.7]))
        many = _compute_confidence(_FakeRegistry(6, 6, [0.7]))
        assert many > few
        assert 0.1 <= few <= 0.95
        assert 0.1 <= many <= 0.95


class TestReplyContractV2V3:
    """Guards the SSE metadata fields the chat bot renders.

    These are the v2/v3 additions the bot depends on; a regression here (e.g.
    reverting confidence to a constant, or dropping a field) would silently
    degrade the bot with no other test catching it.
    """

    @staticmethod
    def _metadata(body: str) -> dict:
        for block in body.split("\n\n"):
            if "event: metadata" in block:
                for line in block.split("\n"):
                    if line.startswith("data:"):
                        return json.loads(line[5:].strip())
        raise AssertionError("no metadata event in stream")

    @pytest.mark.asyncio
    async def test_metadata_carries_computed_confidence_and_signals(
        self, client: AsyncClient, mock_runner
    ):
        response = await client.post(
            "/api/channels/C123/ask",
            json={"question": "what is our stack?"},
        )
        assert response.status_code == 200
        meta = self._metadata(response.text)

        # Confidence is COMPUTED, never the old hardcoded 0.85 constant. The
        # mock runner produces no citations, so the honest value is the 0.15
        # floor (same whether the registry is empty or disabled).
        assert isinstance(meta["confidence"], (int, float))
        assert meta["confidence"] != 0.85
        assert meta["confidence"] == 0.15

        # Honest-empty-state and freshness signals are always present (the bot
        # reads these keys; value may be null/false depending on data).
        assert "is_empty_retrieval" in meta
        assert "last_sync_ts" in meta
        # P1-2: freshness_kind names the semantics of last_sync_ts honestly —
        # it is the last MESSAGE seen, not the last sync RUN.
        assert meta.get("freshness_kind") == "last_message"

    @pytest.mark.asyncio
    async def test_channel_message_full_slack_native_resolves_permalink(self):
        """A channel_message source carrying full Slack native resolves to an
        archives permalink, and the legacy flat `items[].permalink` is populated.

        Exercised at the citation layer (registry + decorator + resolver) because
        the SSE mock runner emits no real tool calls. This is the same pipeline
        the live `/ask` path drives once the registry flag is on.
        """
        from beever_atlas.agents.citations.permalink_resolver import default_resolver
        from beever_atlas.agents.citations.registry import bind, reset
        from beever_atlas.agents.query.stream_rewriter import StreamRewriter
        from beever_atlas.agents.tools._citation_decorator import cite_tool_output

        @cite_tool_output(kind="channel_message")
        async def _search() -> list[dict]:
            return [
                {
                    "text": "we shipped durable media",
                    "author": "alan",
                    "channel_id": "C08TX",
                    "channel_name": "beever",
                    "platform": "slack",
                    "message_ts": "1712500000.001100",
                    "workspace_domain": "beever",
                }
            ]

        r, tok = bind()
        try:
            r.set_permalink_resolver(default_resolver)
            results = await _search()
            rewriter = StreamRewriter(r)
            rewriter.feed(f"Yes {results[0]['_cite']}.")
            rewriter.flush()
            env = r.finalize()
        finally:
            reset(tok)

        expected = "https://beever.slack.com/archives/C08TX/p1712500000001100"
        assert env.sources[0].permalink == expected
        assert env.items[0]["permalink"] == expected
