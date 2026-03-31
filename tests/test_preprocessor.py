from __future__ import annotations

from datetime import UTC, datetime

from beever_atlas.agents.ingestion.preprocessor import _build_thread_context, _is_skippable


def test_is_skippable_accepts_normalized_content_messages() -> None:
    msg = {
        "content": "Hello team",
        "author": "U123",
        "is_bot": False,
    }
    assert _is_skippable(msg) is False


def test_is_skippable_rejects_system_message_from_raw_metadata() -> None:
    msg = {
        "content": "@alice joined the channel",
        "raw_metadata": {"subtype": "channel_join"},
    }
    assert _is_skippable(msg) is True


def test_build_thread_context_supports_normalized_thread_fields() -> None:
    parent_ts = datetime(2026, 3, 20, 12, 0, tzinfo=UTC).isoformat()
    parent = {
        "message_id": parent_ts,
        "author": "U1",
        "author_name": "Alan",
        "content": "Parent message",
    }
    reply = {
        "message_id": datetime(2026, 3, 20, 12, 1, tzinfo=UTC).isoformat(),
        "thread_id": parent_ts,
        "content": "Reply message",
    }
    context = _build_thread_context(reply, {parent_ts: parent})
    assert context == "[Reply to U1: Parent message]"
