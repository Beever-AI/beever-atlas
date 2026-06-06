"""Tests for the bot-reply answer-duplication guards in ``ask.py``.

Covers the two additive dedup layers added in the bot-reply polish work:

1. Event-scoped guard — a single NON-partial (aggregate) ADK event that
   carries the same answer text in two parts must emit exactly ONE
   ``response_delta`` and accumulate the text only once.
2. Finalization safety net (`_dedup_exact_double`) — an answer that is an
   EXACT byte doubling (>= 80 chars) is halved before persistence, while a
   non-exact near-double is left untouched.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.api.ask import _dedup_exact_double
from beever_atlas.server.app import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _install_mock_stores(mock_stores):
    yield mock_stores


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_part(text: str):
    part = MagicMock()
    part.text = text
    part.thought = False
    return part


def _make_event(parts, *, partial: bool = False, turn_complete: bool = False):
    content = MagicMock()
    content.parts = parts
    event = MagicMock()
    event.content = content
    event.partial = partial
    event.turn_complete = turn_complete
    event.error_code = None
    event.error_message = None
    event.get_function_calls.return_value = []
    event.get_function_responses.return_value = []
    return event


async def _noop_decomposed_prompt(question: str, channel_id: str):
    return f"[Channel: {channel_id}]\n\n{question}", None


async def _noop_chat_history(session_id: str):
    return []


def _duplicate_part_runner():
    """Runner that emits one NON-partial event whose two parts repeat the
    same answer text, then a turn_complete event."""
    answer = "The team chose dark mode after a long discussion in the design sync."

    async def run_async(**kwargs):
        yield _make_event([_make_part(answer), _make_part(answer)])
        yield _make_event([_make_part("")], turn_complete=True)

    return run_async, answer


@pytest.fixture
def _mock_session_and_prompt():
    mock_session = MagicMock()
    mock_session.user_id = "test_user"
    mock_session.id = "test_session_id"
    with (
        patch(
            "beever_atlas.agents.query.qa_agent.get_agent_for_mode",
            return_value=MagicMock(),
        ),
        patch("beever_atlas.api.ask.create_runner") as mock_cr,
        patch("beever_atlas.api.ask.create_session", new_callable=AsyncMock) as mock_cs,
        patch(
            "beever_atlas.api.ask._build_decomposed_prompt",
            side_effect=_noop_decomposed_prompt,
        ),
        patch(
            "beever_atlas.api.ask._load_chat_history_parts",
            side_effect=_noop_chat_history,
        ),
    ):
        mock_cs.return_value = mock_session
        yield mock_cr


def _parse_response_deltas(body: str) -> list[str]:
    import json

    deltas = []
    current_event = None
    for line in body.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:]
        elif line.startswith("data: ") and current_event == "response_delta":
            deltas.append(json.loads(line[6:]).get("delta", ""))
            current_event = None
    return deltas


class TestEventScopedGuard:
    @pytest.mark.asyncio
    async def test_duplicate_parts_in_one_event_emit_once(
        self, client: AsyncClient, _mock_session_and_prompt
    ):
        run_async, answer = _duplicate_part_runner()
        runner_instance = MagicMock()
        runner_instance.run_async = run_async
        _mock_session_and_prompt.return_value = runner_instance

        captured = {}
        real_persist = "beever_atlas.api.ask._persist_qa_history"

        async def _capture(**kwargs):
            captured["answer"] = kwargs.get("answer")

        with patch(real_persist, side_effect=_capture):
            response = await client.post(
                "/api/channels/C123/ask",
                json={"question": "what did we decide?"},
            )

        deltas = _parse_response_deltas(response.text)
        # Exactly ONE response_delta carried the answer (the duplicate part
        # in the same aggregate event was suppressed).
        answer_deltas = [d for d in deltas if answer in d]
        assert len(answer_deltas) == 1
        # The accumulated/persisted answer is A, not A+A.
        assert captured.get("answer") == answer


class TestFinalizationNet:
    def test_exact_double_is_halved(self):
        a = "x" * 100
        doubled = a + a  # 200 chars, exact byte doubling
        assert _dedup_exact_double(doubled, "sess") == a

    def test_non_exact_double_is_not_halved(self):
        # "A" + "A " differs in the second half (trailing space) → NOT halved.
        a = "y" * 60
        near = a + a + " "  # second half != first half
        assert _dedup_exact_double(near, "sess") == near

    def test_short_exact_double_below_threshold_untouched(self):
        # Below 80 chars: conservative net does not touch it.
        a = "hello "  # 6 chars; doubled = 12 < 80
        assert _dedup_exact_double(a + a, "sess") == a + a

    def test_single_answer_untouched(self):
        single = (
            "This is a single, non-repeating answer that exceeds eighty characters in total length."
        )
        assert _dedup_exact_double(single, "sess") == single

    def test_citation_renumbered_double_is_halved(self):
        # The real production case: the stateful citation rewriter renumbers the
        # SECOND copy's markers ([1][2] → [3][4]), so a byte-identity check would
        # miss it. The renumbering-aware path must still collapse it and KEEP the
        # first copy's correct numbering.
        c1 = (
            "Team Canada is legitimately good due to the presence of "
            "Shai Gilgeous-Alexander [1]. He strengthens the roster [2]."
        )
        c2 = (
            "Team Canada is legitimately good due to the presence of "
            "Shai Gilgeous-Alexander [3]. He strengthens the roster [4]."
        )
        assert _dedup_exact_double(c1 + c2, "sess") == c1

    def test_legit_repeated_phrase_not_halved(self):
        # An answer that merely repeats a phrase (not the WHOLE answer) is left
        # intact — the halves are not equal even after marker normalization.
        legit = (
            "The 2024 NBA Finals featured the Boston Celtics defeating the Dallas "
            "Mavericks 4-1 [1]. The Boston Celtics are the most recent champions [1]."
        )
        assert _dedup_exact_double(legit, "sess") == legit
