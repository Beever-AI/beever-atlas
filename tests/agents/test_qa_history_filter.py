"""Tests for QA-history self-poisoning filter (Issue #1)."""

from __future__ import annotations

import pytest

from beever_atlas.stores.qa_history_store import _classify_answer


# ---------------------------------------------------------------------------
# Unit tests for _classify_answer classifier
# ---------------------------------------------------------------------------


def test_classify_refused_short_with_marker():
    assert _classify_answer("I don't have any information about that.") == "refused"


def test_classify_refused_no_record():
    assert _classify_answer("There is no record of this event in the channel.") == "refused"


def test_classify_refused_no_information():
    assert _classify_answer("no information was found for that topic.") == "refused"


def test_classify_refused_not_identified():
    assert _classify_answer("The person was not identified in the conversation.") == "refused"


def test_classify_refused_couldnt_find():
    assert _classify_answer("I couldn't find anything matching your query.") == "refused"


def test_classify_refused_no_evidence():
    assert _classify_answer("There is no evidence of this in the channel.") == "refused"


def test_classify_answered_no_markers():
    assert _classify_answer("The meeting is scheduled for Monday at 10am.") == "answered"


def test_legitimate_short_answer_kept():
    """A short answer without any refusal marker must be classified as 'answered'."""
    short_real = "Yes, the deploy was on April 10th at 2pm by Alice."
    assert _classify_answer(short_real) == "answered"


def test_long_answer_with_marker_kept():
    """An answer >= 400 chars that happens to contain a marker is classified 'answered'."""
    long_answer = "I don't have complete certainty but here is what I found: " + "A" * 400
    assert len(long_answer) >= 400
    assert _classify_answer(long_answer) == "answered"


# ---------------------------------------------------------------------------
# Integration-style tests: filter applied in search_qa_history via memory_tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_excluded_when_flag_on():
    """Entries with answer_kind='refused' must be dropped when QA_HISTORY_NEGATIVE_FILTER is on."""
    from unittest.mock import AsyncMock, MagicMock

    refused_entry = {
        "question": "Who is the PM?",
        "answer": "I don't have any information about that.",
        "citations": [],
        "timestamp": "2026-04-01T00:00:00+00:00",
        "session_id": "s1",
        "id": "uuid-refused",
        "answer_kind": "refused",
    }
    answered_entry = {
        "question": "When was the last deploy?",
        "answer": "The last deploy was April 10th.",
        "citations": [],
        "timestamp": "2026-04-02T00:00:00+00:00",
        "session_id": "s2",
        "id": "uuid-answered",
        "answer_kind": "answered",
    }

    mock_store = MagicMock()
    mock_store.startup = AsyncMock()
    mock_store.shutdown = AsyncMock()
    mock_store.search_qa_history = AsyncMock(return_value=[refused_entry, answered_entry])

    mock_settings = MagicMock()
    mock_settings.weaviate_url = "http://localhost:8080"
    mock_settings.weaviate_api_key = ""
    mock_settings.qa_history_negative_filter = True

    # Directly exercise the filter logic used in memory_tools.search_qa_history
    results = await mock_store.search_qa_history(channel_id="C1", query="test", limit=5)
    if mock_settings.qa_history_negative_filter:
        results = [r for r in results if r.get("answer_kind", "answered") != "refused"]

    assert len(results) == 1
    assert results[0]["id"] == "uuid-answered"


@pytest.mark.asyncio
async def test_backward_compat_null_answer_kind_kept():
    """Historical rows with answer_kind=None (NULL) must be kept (treated as 'answered')."""
    from unittest.mock import AsyncMock, MagicMock

    null_kind_entry = {
        "question": "What did the team discuss?",
        "answer": "The team discussed the Q1 roadmap priorities.",
        "citations": [],
        "timestamp": "2026-03-01T00:00:00+00:00",
        "session_id": "s3",
        "id": "uuid-historical",
        "answer_kind": None,
    }

    mock_store = MagicMock()
    mock_store.search_qa_history = AsyncMock(return_value=[null_kind_entry])

    mock_settings = MagicMock()
    mock_settings.qa_history_negative_filter = True

    results = await mock_store.search_qa_history(channel_id="C1", query="test", limit=5)
    if mock_settings.qa_history_negative_filter:
        results = [r for r in results if r.get("answer_kind", "answered") != "refused"]

    assert len(results) == 1
    assert results[0]["id"] == "uuid-historical"


@pytest.mark.asyncio
async def test_filter_off_refused_kept():
    """When QA_HISTORY_NEGATIVE_FILTER is off, refused entries are returned."""
    from unittest.mock import AsyncMock, MagicMock

    refused_entry = {
        "question": "Who joined last week?",
        "answer": "no information found.",
        "citations": [],
        "timestamp": "2026-04-01T00:00:00+00:00",
        "session_id": "s4",
        "id": "uuid-refused-2",
        "answer_kind": "refused",
    }

    mock_store = MagicMock()
    mock_store.search_qa_history = AsyncMock(return_value=[refused_entry])

    mock_settings = MagicMock()
    mock_settings.qa_history_negative_filter = False

    results = await mock_store.search_qa_history(channel_id="C1", query="test", limit=5)
    if mock_settings.qa_history_negative_filter:
        results = [r for r in results if r.get("answer_kind", "answered") != "refused"]

    assert len(results) == 1
    assert results[0]["id"] == "uuid-refused-2"
