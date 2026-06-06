"""Fix 1: meta/recall questions ("what did I first ask you?") must be
recognized AND must not trigger the bot's "nothing indexed" empty state — they
are answered from the prior conversation, which legitimately retrieves no
channel sources."""

from __future__ import annotations

import pytest

from beever_atlas.api.ask import _build_metadata_event, _is_meta_recall_question


@pytest.mark.parametrize(
    "q",
    [
        "what did I first ask you?",
        "what did I originally ask?",
        "my first question",
        "my last message",
        "what was my last question",
        "what did we discuss earlier",
        "do you remember what I said",
        "remind me what I asked",
        "summarize our conversation",
        "recap this conversation",
    ],
)
def test_meta_recall_detected_incl_first_last(q):
    assert _is_meta_recall_question(q) is True


@pytest.mark.parametrize(
    "q",
    [
        "who is SGA?",
        "what is our tech stack?",
        "summarize the Q3 roadmap page",  # about a channel page, not our chat
        "what did the team decide about Postgres",
    ],
)
def test_meta_recall_no_false_positive(q):
    assert _is_meta_recall_question(q) is False


class _EmptyRegistry:
    registered_count = 0
    referenced_count = 0

    def retrieval_scores(self):
        return []


async def test_metadata_suppresses_empty_for_meta_question():
    # registry registered 0 sources (a recall question retrieves no channel data)
    meta = await _build_metadata_event(
        channel_id="C1", session_id="s", mode="deep", registry=_EmptyRegistry(), suppress_empty=True
    )
    normal = await _build_metadata_event(
        channel_id="C1",
        session_id="s",
        mode="deep",
        registry=_EmptyRegistry(),
        suppress_empty=False,
    )
    # Meta: the bot must render the recall answer, NOT the empty state.
    assert meta["is_empty_retrieval"] is False
    # Non-meta empty retrieval still flags the honest empty state.
    assert normal["is_empty_retrieval"] is True
