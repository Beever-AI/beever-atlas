"""Tests for the write_intents.channel_id backfill (delete-channel-v2 Wave 1).

Uses a lightweight in-memory fake mongo client (mirrors
``tests/scripts/test_migrate_wiki_pages_to_slug_identity.py``) so the test
exercises the real ``migrate(...)`` without a live Mongo. Covers:

  * single-channel intent → top-level channel_id backfilled;
  * mixed-channel intent → left None;
  * already-migrated row → untouched (idempotent re-run);
  * pre-migration row (no channel_id field, channel-less facts) → handled,
    not orphaned (left None, never raises);
  * dry-run plans but writes nothing;
  * --channel-id targets only the matching derived channel.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Fake Mongo collection — supports the operators the migrator uses:
#   find({"$or": [{"channel_id": {"$exists": False}}, {"channel_id": None}]})
#   update_one({"id": ...}, {"$set": {...}})
# ---------------------------------------------------------------------------


class _FakeUpdateResult:
    def __init__(self, modified: int) -> None:
        self.modified_count = modified


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakeCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []
        self.update_calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    @staticmethod
    def _matches_clause(doc: dict[str, Any], k: str, v: Any) -> bool:
        if isinstance(v, dict):
            if "$exists" in v:
                present = k in doc
                if present != bool(v["$exists"]):
                    return False
            return True
        return doc.get(k) == v

    @classmethod
    def _matches(cls, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if k == "$or" and isinstance(v, list):
                if not any(cls._matches(doc, branch) for branch in v):
                    return False
                continue
            if not cls._matches_clause(doc, k, v):
                return False
        return True

    def find(self, query: dict[str, Any], projection=None) -> _FakeCursor:
        rows = [dict(d) for d in self.docs if self._matches(d, query)]
        return _FakeCursor(rows)

    async def update_one(self, query: dict[str, Any], update: dict[str, Any]):
        self.update_calls.append((dict(query), dict(update)))
        for doc in self.docs:
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                return _FakeUpdateResult(1)
        return _FakeUpdateResult(0)


class _FakeDB:
    def __init__(self, collection: _FakeCollection) -> None:
        self._collection = collection

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collection


class _FakeClient:
    def __init__(self, collection: _FakeCollection) -> None:
        self._collection = collection

    def __getitem__(self, name: str) -> _FakeDB:
        return _FakeDB(self._collection)

    def close(self) -> None:  # called by migrate's finally
        pass


def _seed(coll: _FakeCollection, rows: list[dict[str, Any]]) -> None:
    coll.docs = [dict(r) for r in rows]


async def _run(coll: _FakeCollection, *, channel_id=None, dry_run=False) -> dict[str, int]:
    with patch(
        "scripts.migrate_write_intent_channel_id.AsyncIOMotorClient",
        return_value=_FakeClient(coll),
    ):
        from scripts.migrate_write_intent_channel_id import migrate

        return await migrate(
            mongodb_uri="mongodb://fake",
            channel_id=channel_id,
            dry_run=dry_run,
        )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_single_channel_intent_backfilled() -> None:
    coll = _FakeCollection()
    _seed(
        coll,
        [
            {
                "id": "i1",
                # no top-level channel_id (pre-migration row)
                "facts": [
                    {"channel_id": "C1", "memory_text": "a"},
                    {"channel_id": "C1", "memory_text": "b"},
                ],
            }
        ],
    )
    counters = await _run(coll)
    assert counters["planned"] == 1
    assert counters["written"] == 1
    assert counters["mixed"] == 0
    assert coll.docs[0]["channel_id"] == "C1"


async def test_mixed_channel_intent_left_none() -> None:
    coll = _FakeCollection()
    _seed(
        coll,
        [
            {
                "id": "i1",
                "facts": [
                    {"channel_id": "C1", "memory_text": "a"},
                    {"channel_id": "C2", "memory_text": "b"},
                ],
            }
        ],
    )
    counters = await _run(coll)
    assert counters["written"] == 0
    assert counters["mixed"] == 1
    # Left untouched — no top-level channel_id written.
    assert "channel_id" not in coll.docs[0]
    assert coll.update_calls == []


async def test_already_migrated_row_untouched_idempotent() -> None:
    coll = _FakeCollection()
    _seed(
        coll,
        [
            {
                "id": "i1",
                "channel_id": "C1",  # already migrated → excluded by query
                "facts": [{"channel_id": "C1", "memory_text": "a"}],
            }
        ],
    )
    counters = await _run(coll)
    assert counters["planned"] == 0
    assert counters["written"] == 0
    assert counters["skipped"] == 0  # query excludes it entirely
    assert coll.update_calls == []
    assert coll.docs[0]["channel_id"] == "C1"


async def test_pre_migration_channelless_facts_not_orphaned() -> None:
    """A pre-migration row with no channel_id field AND facts that carry no
    channel_id must be handled (left None), never raise / orphan."""
    coll = _FakeCollection()
    _seed(
        coll,
        [
            {"id": "i1", "facts": [{"memory_text": "a"}]},  # facts lack channel_id
            {"id": "i2", "facts": []},  # empty facts
            {"id": "i3"},  # no facts key at all
        ],
    )
    counters = await _run(coll)
    assert counters["written"] == 0
    assert counters["mixed"] == 3
    for doc in coll.docs:
        assert "channel_id" not in doc


async def test_explicit_null_channel_id_is_reconsidered() -> None:
    """A row with channel_id explicitly None (not just missing) is matched by
    the query and backfilled when its facts are single-channel."""
    coll = _FakeCollection()
    _seed(
        coll,
        [
            {
                "id": "i1",
                "channel_id": None,
                "facts": [{"channel_id": "C9", "memory_text": "a"}],
            }
        ],
    )
    counters = await _run(coll)
    assert counters["written"] == 1
    assert coll.docs[0]["channel_id"] == "C9"


async def test_idempotent_replay_writes_nothing_second_pass() -> None:
    coll = _FakeCollection()
    _seed(
        coll,
        [
            {"id": "i1", "facts": [{"channel_id": "C1", "memory_text": "a"}]},
            {
                "id": "i2",
                "facts": [
                    {"channel_id": "C1", "memory_text": "x"},
                    {"channel_id": "C2", "memory_text": "y"},
                ],
            },
        ],
    )
    first = await _run(coll)
    assert first["written"] == 1  # i1 backfilled
    assert first["mixed"] == 1  # i2 left None

    second = await _run(coll)
    # i1 now excluded by query; i2 still mixed → still None, no write.
    assert second["written"] == 0
    assert second["mixed"] == 1
    assert coll.docs[0]["channel_id"] == "C1"
    assert "channel_id" not in coll.docs[1]


async def test_dry_run_plans_but_writes_nothing() -> None:
    coll = _FakeCollection()
    _seed(
        coll,
        [{"id": "i1", "facts": [{"channel_id": "C1", "memory_text": "a"}]}],
    )
    counters = await _run(coll, dry_run=True)
    assert counters["planned"] == 1
    assert counters["written"] == 0
    assert coll.update_calls == []
    assert "channel_id" not in coll.docs[0]


async def test_channel_id_filter_targets_only_matching_derivation() -> None:
    coll = _FakeCollection()
    _seed(
        coll,
        [
            {"id": "i1", "facts": [{"channel_id": "C1", "memory_text": "a"}]},
            {"id": "i2", "facts": [{"channel_id": "C2", "memory_text": "b"}]},
        ],
    )
    counters = await _run(coll, channel_id="C1")
    assert counters["written"] == 1
    by_id = {d["id"]: d for d in coll.docs}
    assert by_id["i1"]["channel_id"] == "C1"
    # C2 row not touched.
    assert "channel_id" not in by_id["i2"]
