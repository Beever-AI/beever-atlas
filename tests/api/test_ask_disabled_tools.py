"""Tests for per-request tool filtering via AskRequest.disabled_tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.agents.tools import QA_TOOLS
from beever_atlas.server.app import app


def _tool_name(tool) -> str:
    return (
        getattr(tool, "__name__", None)
        or getattr(tool, "name", None)
        or getattr(getattr(tool, "func", None), "__name__", "")
    )


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_terminal_event():
    event = MagicMock()
    event.content = None
    event.partial = False
    event.turn_complete = True
    event.error_code = None
    event.error_message = None
    event.get_function_calls.return_value = []
    event.get_function_responses.return_value = []
    return event


async def _mock_run_async(**kwargs):
    yield _make_terminal_event()


async def _noop_decomposed_prompt(question: str, channel_id: str):
    return f"[Channel: {channel_id}]\n\n{question}", None


async def _noop_chat_history(session_id: str):
    return []


class _AgentStreamPatches:
    """Context-manager bundle of patches needed to exercise the ask endpoint
    without hitting real stores, LLM, or ADK internals."""

    def __init__(self, create_agent_mock):
        self.create_agent_mock = create_agent_mock
        self._stack: list = []

    def __enter__(self):
        mock_session = MagicMock()
        mock_session.user_id = "test_user"
        mock_session.id = "test_session_id"

        mock_runner = MagicMock()
        mock_runner.run_async = _mock_run_async

        patches = [
            patch(
                "beever_atlas.agents.query.qa_agent.create_qa_agent",
                side_effect=self.create_agent_mock,
            ),
            patch(
                "beever_atlas.agents.query.qa_agent.get_agent_for_mode",
                return_value=MagicMock(),
            ),
            patch("beever_atlas.api.ask.create_runner", return_value=mock_runner),
            patch(
                "beever_atlas.api.ask.create_session",
                new_callable=AsyncMock,
                return_value=mock_session,
            ),
            patch(
                "beever_atlas.api.ask._build_decomposed_prompt",
                side_effect=_noop_decomposed_prompt,
            ),
            patch(
                "beever_atlas.api.ask._load_chat_history_parts",
                side_effect=_noop_chat_history,
            ),
            patch(
                "beever_atlas.api.ask._persist_qa_history",
                new_callable=AsyncMock,
            ),
        ]
        self._active = [p.__enter__() for p in patches]
        self._stack = patches
        return self

    def __exit__(self, exc_type, exc, tb):
        for p in reversed(self._stack):
            p.__exit__(exc_type, exc, tb)


@pytest.mark.anyio
async def test_disabled_tools_filtered_from_agent(client, mock_stores):
    captured: dict = {}

    def fake_create_qa_agent(mode="deep", tools=None, extra_instruction=""):
        captured["mode"] = mode
        captured["tools"] = tools
        captured["extra_instruction"] = extra_instruction
        return MagicMock()

    with _AgentStreamPatches(fake_create_qa_agent):
        resp = await client.post(
            "/api/ask",
            json={
                "question": "hello",
                "channel_id": "C123",
                "disabled_tools": ["search_channel_facts"],
            },
        )
        # Drain SSE stream
        async for _ in resp.aiter_bytes():
            pass

    assert captured.get("tools") is not None, "create_qa_agent was not called with tools"
    tool_names = {_tool_name(t) for t in captured["tools"]}
    assert "search_channel_facts" not in tool_names
    # The rest of the registry should still be present.
    assert "get_wiki_page" in tool_names


@pytest.mark.anyio
async def test_qa_tools_not_mutated(client, mock_stores):
    before_identity = id(QA_TOOLS)
    before_list = list(QA_TOOLS)

    def fake_create_qa_agent(mode="deep", tools=None, extra_instruction=""):
        return MagicMock()

    with _AgentStreamPatches(fake_create_qa_agent):
        resp = await client.post(
            "/api/ask",
            json={
                "question": "hello",
                "channel_id": "C123",
                "disabled_tools": ["search_channel_facts", "find_experts"],
            },
        )
        async for _ in resp.aiter_bytes():
            pass

    assert id(QA_TOOLS) == before_identity
    assert QA_TOOLS == before_list


@pytest.mark.anyio
async def test_unknown_tool_name_ignored(client, mock_stores):
    def fake_create_qa_agent(mode="deep", tools=None, extra_instruction=""):
        return MagicMock()

    from beever_atlas.api import ask as ask_module

    with _AgentStreamPatches(fake_create_qa_agent):
        with patch.object(ask_module.logger, "warning") as mock_warn:
            resp = await client.post(
                "/api/ask",
                json={
                    "question": "hello",
                    "channel_id": "C123",
                    "disabled_tools": ["nonexistent_tool"],
                },
            )
            async for _ in resp.aiter_bytes():
                pass

    assert resp.status_code == 200
    warn_calls = [
        (args, kwargs) for args, kwargs in (c[0:2] for c in mock_warn.call_args_list)
    ]
    rendered = []
    for args, _kwargs in warn_calls:
        if not args:
            continue
        fmt = args[0]
        try:
            rendered.append(fmt % args[1:] if len(args) > 1 else fmt)
        except TypeError:
            rendered.append(str(args))
    assert any("nonexistent_tool" in m for m in rendered), (
        f"Expected warning about nonexistent_tool; got: {rendered}"
    )


@pytest.mark.anyio
async def test_refusal_clause_appended(client, mock_stores):
    captured: dict = {}

    def fake_create_qa_agent(mode="deep", tools=None, extra_instruction=""):
        captured["extra_instruction"] = extra_instruction
        return MagicMock()

    disabled = ["search_channel_facts", "find_experts"]

    with _AgentStreamPatches(fake_create_qa_agent):
        resp = await client.post(
            "/api/ask",
            json={
                "question": "hello",
                "channel_id": "C123",
                "disabled_tools": disabled,
            },
        )
        async for _ in resp.aiter_bytes():
            pass

    clause = captured.get("extra_instruction", "")
    assert "disabled" in clause.lower()
    for name in disabled:
        assert name in clause, f"Expected {name!r} in refusal clause"
