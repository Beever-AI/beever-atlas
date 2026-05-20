"""Tests for ``ChatHistoryStore.delete_by_channel`` (delete-channel-v2 Wave 1).

Dual-schema hard delete:
  * v1 (top-level channel_id) → whole session removed.
  * v2 (per-message channel_id) → this channel's messages ``$pull``ed; a
    session left empty *by the purge* is then removed. Mixed-channel v2
    sessions keep their other-channel messages (and survive).

Return value = session DOCUMENTS removed (v1 dropped + v2 emptied).

No live Mongo / mongomock — a focused fake collection implements just the
operators ``delete_by_channel`` uses: ``delete_many`` (equality, ``$exists``,
``$size``, ``$in``), ``update_many`` (``$pull`` on a sub-doc match + ``$set``),
and ``find`` (``messages.channel_id`` array-element match).

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.stores.chat_history_store import ChatHistoryStore


class _DeleteResult:
    def __init__(self, n: int) -> None:
        self.deleted_count = n


class _Cursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakeChatColl:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs: list[dict[str, Any]] = [dict(d) for d in docs]

    # -- matching -----------------------------------------------------------
    @classmethod
    def _match_clause(cls, doc: dict[str, Any], k: str, v: Any) -> bool:
        if k == "messages.channel_id":
            msgs = doc.get("messages") or []
            return any(isinstance(m, dict) and m.get("channel_id") == v for m in msgs)
        if k == "messages" and isinstance(v, dict) and "$size" in v:
            return len(doc.get("messages") or []) == v["$size"]
        actual = doc.get(k)
        if isinstance(v, dict):
            if "$exists" in v:
                present = k in doc
                if present != bool(v["$exists"]):
                    return False
            if "$in" in v:
                if actual not in v["$in"]:
                    return False
            return True
        return actual == v

    @classmethod
    def _matches(cls, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(cls._match_clause(doc, k, v) for k, v in query.items())

    # -- ops ----------------------------------------------------------------
    async def delete_many(self, query: dict[str, Any]) -> _DeleteResult:
        keep, removed = [], 0
        for doc in self.docs:
            if self._matches(doc, query):
                removed += 1
            else:
                keep.append(doc)
        self.docs = keep
        return _DeleteResult(removed)

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]):
        for doc in self.docs:
            if not self._matches(doc, query):
                continue
            pull = update.get("$pull", {})
            if "messages" in pull:
                cond = pull["messages"]
                doc["messages"] = [
                    m
                    for m in (doc.get("messages") or [])
                    if not all(m.get(ck) == cv for ck, cv in cond.items())
                ]
            for k, v in update.get("$set", {}).items():
                doc[k] = v

    def find(self, query: dict[str, Any], projection=None) -> _Cursor:
        rows = [dict(d) for d in self.docs if self._matches(d, query)]
        return _Cursor(rows)


def _store(docs: list[dict[str, Any]]) -> tuple[ChatHistoryStore, _FakeChatColl]:
    store = ChatHistoryStore.__new__(ChatHistoryStore)
    coll = _FakeChatColl(docs)
    store._collection = coll  # type: ignore[attr-defined]
    return store, coll


async def test_v1_session_deleted_whole() -> None:
    store, coll = _store(
        [
            {"session_id": "s1", "channel_id": "C1", "messages": [{"role": "user"}]},
            {"session_id": "s2", "channel_id": "C2", "messages": [{"role": "user"}]},
        ]
    )
    deleted = await store.delete_by_channel("C1")
    assert deleted == 1
    assert [d["session_id"] for d in coll.docs] == ["s2"]


async def test_v2_session_emptied_then_deleted() -> None:
    """A v2 session holding ONLY the purged channel's messages is removed."""
    store, coll = _store(
        [
            {
                "session_id": "s1",  # no top-level channel_id (v2)
                "messages": [
                    {"role": "user", "channel_id": "C1"},
                    {"role": "assistant", "channel_id": "C1"},
                ],
            }
        ]
    )
    deleted = await store.delete_by_channel("C1")
    assert deleted == 1
    assert coll.docs == []


async def test_v2_mixed_session_trimmed_not_deleted() -> None:
    """A v2 session with other channels' messages survives; only C1's go.

    Trimmed-but-surviving sessions are NOT counted in the return value.
    """
    store, coll = _store(
        [
            {
                "session_id": "s1",
                "messages": [
                    {"role": "user", "channel_id": "C1"},
                    {"role": "assistant", "channel_id": "C2"},
                ],
            }
        ]
    )
    deleted = await store.delete_by_channel("C1")
    assert deleted == 0  # session survived → not counted
    assert len(coll.docs) == 1
    remaining = coll.docs[0]["messages"]
    assert [m["channel_id"] for m in remaining] == ["C2"]


async def test_brand_new_empty_v2_session_not_deleted() -> None:
    """A freshly-created v2 session with an empty messages array that never
    carried this channel must NOT be swept by the empty-session cleanup."""
    store, coll = _store(
        [
            {"session_id": "fresh", "messages": []},  # never had C1
            {
                "session_id": "s1",
                "messages": [{"role": "user", "channel_id": "C1"}],
            },
        ]
    )
    deleted = await store.delete_by_channel("C1")
    assert deleted == 1  # only s1 (emptied by purge)
    assert [d["session_id"] for d in coll.docs] == ["fresh"]


async def test_cross_channel_survival_mixed_v1_v2() -> None:
    store, coll = _store(
        [
            {"session_id": "v1a", "channel_id": "C1", "messages": [{"role": "u"}]},
            {"session_id": "v1b", "channel_id": "C2", "messages": [{"role": "u"}]},
            {
                "session_id": "v2only",
                "messages": [{"role": "u", "channel_id": "C1"}],
            },
            {
                "session_id": "v2mixed",
                "messages": [
                    {"role": "u", "channel_id": "C1"},
                    {"role": "a", "channel_id": "C2"},
                ],
            },
        ]
    )
    deleted = await store.delete_by_channel("C1")
    # v1a deleted + v2only emptied = 2 documents removed.
    assert deleted == 2
    survivors = {d["session_id"] for d in coll.docs}
    assert survivors == {"v1b", "v2mixed"}
    mixed = next(d for d in coll.docs if d["session_id"] == "v2mixed")
    assert [m["channel_id"] for m in mixed["messages"]] == ["C2"]


async def test_no_matching_channel_is_noop() -> None:
    store, coll = _store([{"session_id": "s1", "channel_id": "C2", "messages": [{"role": "u"}]}])
    deleted = await store.delete_by_channel("C1")
    assert deleted == 0
    assert len(coll.docs) == 1
