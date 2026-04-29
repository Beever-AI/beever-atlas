"""Tests for `MongoDBStore.get_channel_sync_states_batch` resilience to
corrupt documents (issue #40).

Verifies:
  * valid documents are deserialized and returned
  * documents that fail Pydantic validation are SKIPPED (existing behaviour)
  * skipped documents now emit a WARNING log via the project logger so
    operators can diagnose silent channel disappearances (the fix)

Uses a hand-rolled async cursor stub instead of motor — keeps the test
fast and lets it run without a live MongoDB.

No `@pytest.mark.asyncio` decorators per project convention
(`asyncio_mode = "auto"`).
"""

from __future__ import annotations

import beever_atlas.stores.mongodb_store as mongo_mod
from beever_atlas.stores.mongodb_store import MongoDBStore


def _make_valid_doc(channel_id: str) -> dict:
    """Produce a doc shaped like ChannelSyncState's persisted form
    (`models/persistence.py:57` — keep in sync if the model adds required fields)."""
    return {
        "channel_id": channel_id,
        "last_sync_ts": "2026-04-01T00:00:00+00:00",
        "total_synced_messages": 0,
        "primary_language": "en",
        "primary_language_confidence": 0.95,
    }


class _AsyncCursorStub:
    """Minimal async iterator over a list — mimics motor's `find()` cursor."""

    def __init__(self, docs: list[dict]) -> None:
        self._docs = list(docs)

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


def _store_with_collection(docs: list[dict]) -> MongoDBStore:
    """Build a MongoDBStore with `_channel_sync_state.find(...)` returning
    the supplied docs. Bypass __init__ so we don't need a live mongo URI."""

    class _Coll:
        def find(self, *_args, **_kwargs):  # noqa: ANN001 — test stub
            return _AsyncCursorStub(docs)

    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_sync_state = _Coll()  # type: ignore[attr-defined]
    return store


# ── Tests ───────────────────────────────────────────────────────────────


async def test_batch_returns_valid_states() -> None:
    store = _store_with_collection([_make_valid_doc("C_GOOD")])
    result = await store.get_channel_sync_states_batch(["C_GOOD"])
    assert "C_GOOD" in result
    assert result["C_GOOD"].channel_id == "C_GOOD"


async def test_batch_skips_corrupt_docs_and_logs_warning(monkeypatch) -> None:
    """A doc that fails Pydantic validation is skipped AND the failure is
    logged at WARNING level (issue #40 fix — the bare `except: continue`
    used to swallow these silently)."""
    captured: list[str] = []
    monkeypatch.setattr(
        mongo_mod.logger,
        "warning",
        lambda msg, *a, **kw: captured.append(msg % a if a else msg),
    )

    valid = _make_valid_doc("C_GOOD")
    # `synced_count` is `int`; "not-a-number" forces ValidationError.
    corrupt = _make_valid_doc("C_BAD")
    corrupt["total_synced_messages"] = "not-a-number"

    store = _store_with_collection([valid, corrupt])
    result = await store.get_channel_sync_states_batch(["C_GOOD", "C_BAD"])

    # Valid one is present; corrupt one is skipped.
    assert set(result.keys()) == {"C_GOOD"}
    # WARNING log fires with the corrupt channel id.
    assert any("C_BAD" in m for m in captured), (
        f"expected corrupt channel id in WARNING log; got {captured}"
    )
    assert any("get_channel_sync_states_batch" in m for m in captured)


async def test_batch_handles_all_corrupt_without_raising() -> None:
    """If every doc is corrupt, return empty dict (do not raise)."""
    bad = _make_valid_doc("C_BAD")
    bad["total_synced_messages"] = "x"  # ValidationError
    store = _store_with_collection([bad])
    result = await store.get_channel_sync_states_batch(["C_BAD"])
    assert result == {}


async def test_batch_empty_input_short_circuits() -> None:
    """Empty channel_ids returns {} without querying."""
    store = _store_with_collection([_make_valid_doc("C_GOOD")])
    result = await store.get_channel_sync_states_batch([])
    assert result == {}
