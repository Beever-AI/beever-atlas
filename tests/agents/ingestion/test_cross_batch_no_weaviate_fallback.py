"""Regression tests for `_resolve_cross_batch_parent` — issue #44.

Asserts the corrected contract:
  * MongoDB hit → returns `[Reply to <author>: <text>]`
  * MongoDB miss → returns None (NOT the previous Weaviate fallback that
    yielded an unrelated fact from the channel)
  * Disabled flag → returns None
  * In-batch resolution → returns None (parent already in `messages_by_ts`)

Uses AsyncMock for the MongoDB collection so the test runs without a
live MongoDB.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from beever_atlas.agents.ingestion import preprocessor as pre_mod
from beever_atlas.agents.ingestion.preprocessor import _resolve_cross_batch_parent


def _stores_with(mongodb_record: dict | None) -> MagicMock:
    """Build a fake `stores` whose `mongodb.db['raw_messages'].find_one`
    returns the supplied record (or None for a miss)."""
    stores = MagicMock()
    stores.mongodb.db = {"raw_messages": MagicMock()}
    stores.mongodb.db["raw_messages"].find_one = AsyncMock(return_value=mongodb_record)
    # Issue #44 verification: weaviate must NOT be touched. Wire up a
    # MagicMock and assert no calls were made on it after the function
    # returns.
    stores.weaviate.list_facts = AsyncMock(
        side_effect=AssertionError(
            "weaviate.list_facts must NOT be called — issue #44 removed the fallback"
        ),
    )
    return stores


def _settings_with(thread_context_enabled: bool = True) -> MagicMock:
    s = MagicMock()
    s.cross_batch_thread_context_enabled = thread_context_enabled
    s.thread_context_max_length = 200
    return s


# ── Tests ───────────────────────────────────────────────────────────────


async def test_mongodb_hit_returns_reply_context(monkeypatch) -> None:
    """When MongoDB has the parent message, format and return the
    [Reply to <author>: <text>] context string."""
    record = {
        "message_ts": "1700000000.000100",
        "author_name": "alice",
        "text": "Original parent message",
    }
    stores = _stores_with(record)
    monkeypatch.setattr(pre_mod, "get_settings", lambda: _settings_with())
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)

    result = await _resolve_cross_batch_parent(
        msg={
            "thread_ts": "1700000000.000100",
            "message_ts": "1700000001.000200",
            "channel_id": "C_TEST",
        },
        messages_by_ts={},
    )

    assert result == "[Reply to alice: Original parent message]"


async def test_mongodb_miss_returns_none_not_fallback(monkeypatch) -> None:
    """Issue #44 — when MongoDB has no record, return None instead of
    falling back to an unrelated Weaviate fact. The fake `stores.weaviate.list_facts`
    raises AssertionError if called, so this test fails loudly if the
    fallback ever returns."""
    stores = _stores_with(None)
    monkeypatch.setattr(pre_mod, "get_settings", lambda: _settings_with())
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)

    result = await _resolve_cross_batch_parent(
        msg={
            "thread_ts": "1700000000.000100",
            "message_ts": "1700000001.000200",
            "channel_id": "C_TEST",
        },
        messages_by_ts={},
    )

    assert result is None
    # Assert the broken fallback was NOT exercised.
    stores.weaviate.list_facts.assert_not_called()


async def test_disabled_flag_returns_none(monkeypatch) -> None:
    """Feature-flag disabled → short-circuit None before any store call."""
    stores = _stores_with(None)
    monkeypatch.setattr(
        pre_mod, "get_settings", lambda: _settings_with(thread_context_enabled=False)
    )
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)

    result = await _resolve_cross_batch_parent(
        msg={
            "thread_ts": "1700000000.000100",
            "message_ts": "1700000001.000200",
            "channel_id": "C_TEST",
        },
        messages_by_ts={},
    )

    assert result is None
    stores.mongodb.db["raw_messages"].find_one.assert_not_called()


async def test_in_batch_resolution_returns_none(monkeypatch) -> None:
    """If `thread_ts` is already in `messages_by_ts`, no cross-batch
    lookup is needed — return None."""
    stores = _stores_with(None)
    monkeypatch.setattr(pre_mod, "get_settings", lambda: _settings_with())
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)

    result = await _resolve_cross_batch_parent(
        msg={
            "thread_ts": "1700000000.000100",
            "message_ts": "1700000001.000200",
            "channel_id": "C_TEST",
        },
        messages_by_ts={"1700000000.000100": {"text": "in-batch parent"}},
    )

    assert result is None
    stores.mongodb.db["raw_messages"].find_one.assert_not_called()
