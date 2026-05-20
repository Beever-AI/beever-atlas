"""Tests for the channel hard-purge lock CAS primitives on MongoDBStore.

delete-channel-v2 Wave 0. Covers:

  * ``claim_purge`` grants the lock exactly once (CAS), a second concurrent
    claim loses, a STALE lock is reclaimable, the DuplicateKey upsert race
    returns False.
  * ``release_purge`` deletes the lock doc.
  * ``is_purging`` / ``get_purging_channel_ids`` / ``list_stale_purge_locks``
    honour the staleness boundary.

No live Mongo — a lightweight fake collection mimics motor's surface for
``find_one_and_update`` (upsert + ``$or`` over ``started_at``), ``find_one``,
``find``, and ``delete_one``, plus the unique-index ``DuplicateKeyError`` the
real collection raises on a concurrent insert race.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from pymongo.errors import DuplicateKeyError

from beever_atlas.stores.mongodb_store import (
    PURGE_LOCK_STALE_AFTER_S,
    MongoDBStore,
)


# ─────────────────────────────────────────────────────────────────────────────
# Lightweight fake collection mimicking the channel_purge_locks surface
# ─────────────────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def __aiter__(self):
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakePurgeLocks:
    """In-memory stand-in keyed by ``channel_id`` (the unique index).

    Implements just enough query semantics for the purge-lock helpers:
    equality, ``$lt`` / ``$gte`` / ``$exists`` over ``started_at``, and the
    ``$or`` the CAS filter uses. ``find_one_and_update`` with ``upsert=True``
    inserts when no doc matches the filter — and raises ``DuplicateKeyError``
    when a (non-matching) row already occupies the unique ``channel_id`` slot,
    exactly like a real unique-index upsert race.
    """

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}
        # When set, the NEXT find_one_and_update upsert-insert raises
        # DuplicateKeyError to simulate the concurrent-insert race.
        self.raise_dup_on_next_insert = False

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if k == "$or" and isinstance(v, list):
                if not any(_FakePurgeLocks._matches(doc, branch) for branch in v):
                    return False
                continue
            if isinstance(v, dict):
                if "$exists" in v:
                    present = k in doc and doc.get(k) is not None
                    if present != bool(v["$exists"]):
                        return False
                if "$lt" in v and not (doc.get(k) is not None and doc.get(k) < v["$lt"]):
                    return False
                if "$gte" in v and not (doc.get(k) is not None and doc.get(k) >= v["$gte"]):
                    return False
            else:
                if doc.get(k) != v:
                    return False
        return True

    async def find_one_and_update(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
        return_document: Any = None,
    ) -> dict[str, Any] | None:
        # Try to match an existing doc against the full filter.
        for doc in self._docs.values():
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                # $setOnInsert is ignored on an update of an existing doc.
                return {**doc}
        if not upsert:
            return None
        # Upsert-insert path. A real unique index raises DuplicateKeyError
        # when a row already occupies the channel_id slot (the filter failed
        # only because the existing lock is FRESH, not because the key is
        # free). Simulate that, plus the explicit race toggle.
        channel_id = query.get("channel_id")
        if channel_id in self._docs or self.raise_dup_on_next_insert:
            self.raise_dup_on_next_insert = False
            raise DuplicateKeyError("E11000 duplicate key channel_id")
        new_doc: dict[str, Any] = {}
        for k, v in update.get("$setOnInsert", {}).items():
            new_doc[k] = v
        for k, v in update.get("$set", {}).items():
            new_doc[k] = v
        self._docs[channel_id] = new_doc
        return {**new_doc}

    async def find_one(
        self, query: dict[str, Any], projection: Any = None
    ) -> dict[str, Any] | None:
        for doc in self._docs.values():
            if self._matches(doc, query):
                return {**doc}
        return None

    def find(self, query: dict[str, Any], projection: Any = None) -> _FakeCursor:
        rows = [{**doc} for doc in self._docs.values() if self._matches(doc, query)]
        return _FakeCursor(rows)

    async def delete_one(self, query: dict[str, Any]) -> None:
        for key, doc in list(self._docs.items()):
            if self._matches(doc, query):
                del self._docs[key]
                return


def _store_with_fake() -> tuple[MongoDBStore, _FakePurgeLocks]:
    fake = _FakePurgeLocks()
    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_purge_locks = fake  # type: ignore[attr-defined]
    return store, fake


def _age(doc: dict[str, Any], seconds: float) -> None:
    """Backdate a lock's ``started_at`` by ``seconds`` to simulate staleness."""
    doc["started_at"] = datetime.now(tz=UTC) - timedelta(seconds=seconds)


# ─────────────────────────────────────────────────────────────────────────────
# claim_purge CAS semantics
# ─────────────────────────────────────────────────────────────────────────────


async def test_claim_purge_grants_once() -> None:
    store, fake = _store_with_fake()
    granted = await store.claim_purge("C1", stale_after_s=900, owner_principal_id="user:abc")
    assert granted is True
    assert "C1" in fake._docs
    assert fake._docs["C1"]["state"] == "purging"
    assert fake._docs["C1"]["channel_id"] == "C1"
    assert fake._docs["C1"]["owner_principal_id"] == "user:abc"


async def test_second_concurrent_claim_loses() -> None:
    """A second claim while a FRESH lock is held returns False (no double-purge)."""
    store, _ = _store_with_fake()
    first = await store.claim_purge("C1", stale_after_s=900)
    second = await store.claim_purge("C1", stale_after_s=900)
    assert first is True
    assert second is False


async def test_stale_lock_is_reclaimable() -> None:
    """A lock older than ``stale_after_s`` is reclaimed by the next claim."""
    store, fake = _store_with_fake()
    assert await store.claim_purge("C1", stale_after_s=900) is True
    # Backdate the lock past the staleness window.
    _age(fake._docs["C1"], 1000)
    reclaimed = await store.claim_purge("C1", stale_after_s=900)
    assert reclaimed is True
    # started_at was refreshed to ~now.
    assert (datetime.now(tz=UTC) - fake._docs["C1"]["started_at"]).total_seconds() < 5


async def test_duplicate_key_race_returns_false() -> None:
    """A concurrent insert that raises DuplicateKeyError is treated as 'lost'."""
    store, fake = _store_with_fake()
    fake.raise_dup_on_next_insert = True
    granted = await store.claim_purge("C1", stale_after_s=900)
    assert granted is False
    assert "C1" not in fake._docs


# ─────────────────────────────────────────────────────────────────────────────
# release_purge
# ─────────────────────────────────────────────────────────────────────────────


async def test_release_purge_deletes_lock() -> None:
    store, fake = _store_with_fake()
    await store.claim_purge("C1", stale_after_s=900)
    assert "C1" in fake._docs
    await store.release_purge("C1")
    assert "C1" not in fake._docs


async def test_release_purge_missing_is_noop() -> None:
    store, _ = _store_with_fake()
    # Must not raise on a non-existent lock.
    await store.release_purge("nope")


async def test_release_then_reclaim_starts_clean() -> None:
    store, fake = _store_with_fake()
    assert await store.claim_purge("C1", stale_after_s=900) is True
    await store.release_purge("C1")
    # After release the channel is free — a fresh claim wins again.
    assert await store.claim_purge("C1", stale_after_s=900) is True
    assert "C1" in fake._docs


# ─────────────────────────────────────────────────────────────────────────────
# is_purging / get_purging_channel_ids / list_stale_purge_locks
# ─────────────────────────────────────────────────────────────────────────────


async def test_is_purging_true_for_fresh_lock() -> None:
    store, _ = _store_with_fake()
    await store.claim_purge("C1", stale_after_s=900)
    assert await store.is_purging("C1", stale_after_s=900) is True
    assert await store.is_purging("other", stale_after_s=900) is False


async def test_is_purging_false_for_stale_lock() -> None:
    store, fake = _store_with_fake()
    await store.claim_purge("C1", stale_after_s=900)
    _age(fake._docs["C1"], 1000)
    assert await store.is_purging("C1", stale_after_s=900) is False


async def test_get_purging_channel_ids_excludes_stale() -> None:
    store, fake = _store_with_fake()
    await store.claim_purge("fresh", stale_after_s=900)
    await store.claim_purge("stale", stale_after_s=900)
    _age(fake._docs["stale"], 1000)
    ids = await store.get_purging_channel_ids(stale_after_s=900)
    assert ids == {"fresh"}


async def test_list_stale_purge_locks_returns_only_stale() -> None:
    store, fake = _store_with_fake()
    await store.claim_purge("fresh", stale_after_s=900)
    await store.claim_purge("stale", stale_after_s=900)
    _age(fake._docs["stale"], 2000)
    stale = await store.list_stale_purge_locks(older_than_s=900)
    assert stale == ["stale"]


async def test_default_stale_threshold_constant() -> None:
    """The module default is the documented 15 minutes (should-fix #5)."""
    assert PURGE_LOCK_STALE_AFTER_S == 900.0


# ─────────────────────────────────────────────────────────────────────────────
# Concurrent double-purge — exactly one winner via asyncio.gather (AC#12-style)
# ─────────────────────────────────────────────────────────────────────────────


async def test_concurrent_double_claim_exactly_one_winner() -> None:
    """Two ``claim_purge`` calls for the same channel: exactly one wins.

    The fake runs cooperatively, but the CAS contract (filter + unique-index
    upsert) must yield one True and one False regardless of interleaving.
    """
    import asyncio

    store, _ = _store_with_fake()
    results = await asyncio.gather(
        store.claim_purge("C1", stale_after_s=900),
        store.claim_purge("C1", stale_after_s=900),
    )
    assert sorted(results) == [False, True]
