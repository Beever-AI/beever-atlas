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


def _discord_webhook_persona_msg() -> dict:
    """A Discord webhook 'persona' message: human content, but Discord stamps
    ``author.bot=true`` so the bridge stores ``raw_metadata.is_bot=true`` and the
    message is posted flat (no thread reference)."""
    return {
        "content": "We should book the ryokan early — the good ones sell out months ahead.",
        "author_name": "Ken Lau",
        "is_bot": False,
        "raw_metadata": {"is_bot": True},
    }


def test_is_skippable_drops_webhook_bot_message_by_default() -> None:
    # Default behaviour: a flat bot-authored message is integration noise -> drop.
    assert _is_skippable(_discord_webhook_persona_msg()) is True


def test_is_skippable_keeps_webhook_bot_message_when_flag_set() -> None:
    # With ingest_bot_messages on, bot authorship alone must not skip it.
    assert _is_skippable(_discord_webhook_persona_msg(), keep_bot_messages=True) is False


def test_is_skippable_flag_still_drops_empty_and_system_messages() -> None:
    # The flag only relaxes the bot check — empty-text and system subtypes still skip.
    empty = {"content": "   ", "raw_metadata": {"is_bot": True}}
    system = {
        "content": "@alice joined",
        "raw_metadata": {"is_bot": True, "subtype": "channel_join"},
    }
    assert _is_skippable(empty, keep_bot_messages=True) is True
    assert _is_skippable(system, keep_bot_messages=True) is True


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
