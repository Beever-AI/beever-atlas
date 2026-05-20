"""Tests for ``MongoDBStore.purge_channel`` (delete-channel-v2 Wave 1).

Aggregator that hard-deletes every Mongo document the store owns for a
channel and returns per-collection counts. Covers:

  * per-collection delete counts are returned for the right collections;
  * other channels' rows survive;
  * the ``activity_events`` ``$or`` matches both top-level ``channel_id`` and
    nested ``details.channel_id``;
  * ``imported_messages`` is reached best-effort and a failure there does not
    abort the rest of the purge;
  * ``clear_channel_sync_state`` semantics (sync_state + sync_jobs cleared)
    are honoured and counted.

No live Mongo — a lightweight fake collection mimics the motor surface the
method uses (``delete_many`` with equality / ``$or`` / ``details.channel_id``,
``count_documents``, ``delete_one``).

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.stores.mongodb_store import MongoDBStore


class _DeleteResult:
    def __init__(self, n: int) -> None:
        self.deleted_count = n


class _FakeCollection:
    """In-memory collection supporting the operators ``purge_channel`` uses."""

    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs: list[dict[str, Any]] = [dict(d) for d in (docs or [])]
        self.fail_on_delete = False

    @staticmethod
    def _get_path(doc: dict[str, Any], dotted: str) -> Any:
        cur: Any = doc
        for part in dotted.split("."):
            if not isinstance(cur, dict):
                return None
            cur = cur.get(part)
        return cur

    @classmethod
    def _matches(cls, doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if k == "$or" and isinstance(v, list):
                if not any(cls._matches(doc, branch) for branch in v):
                    return False
                continue
            if cls._get_path(doc, k) != v:
                return False
        return True

    async def delete_many(self, query: dict[str, Any]) -> _DeleteResult:
        if self.fail_on_delete:
            raise RuntimeError("simulated driver failure")
        keep: list[dict[str, Any]] = []
        removed = 0
        for doc in self.docs:
            if self._matches(doc, query):
                removed += 1
            else:
                keep.append(doc)
        self.docs = keep
        return _DeleteResult(removed)

    async def delete_one(self, query: dict[str, Any]) -> _DeleteResult:
        for i, doc in enumerate(self.docs):
            if self._matches(doc, query):
                del self.docs[i]
                return _DeleteResult(1)
        return _DeleteResult(0)

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for doc in self.docs if self._matches(doc, query))


class _FakeDB:
    """Exposes ``db[...]`` for collections accessed via ``_db`` rather than a
    store attr: the best-effort legacy ``imported_messages`` path and the
    purge-only ``wiki_versions`` snapshot delete."""

    def __init__(
        self,
        imported: _FakeCollection,
        wiki_versions: _FakeCollection,
        wiki_version_counters: _FakeCollection,
    ) -> None:
        self._collections = {
            "imported_messages": imported,
            "wiki_versions": wiki_versions,
            "wiki_version_counters": wiki_version_counters,
        }

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._collections[name]


def _store_with_fakes() -> tuple[MongoDBStore, dict[str, _FakeCollection]]:
    store = MongoDBStore.__new__(MongoDBStore)
    colls: dict[str, _FakeCollection] = {}

    def _seed(name: str, docs: list[dict[str, Any]]) -> _FakeCollection:
        c = _FakeCollection(docs)
        colls[name] = c
        return c

    cid, other = "C1", "C2"

    store._channel_messages = _seed(  # type: ignore[attr-defined]
        "channel_messages",
        [{"channel_id": cid}, {"channel_id": cid}, {"channel_id": other}],
    )
    imported = _seed(
        "imported_messages",
        [{"channel_id": cid}, {"channel_id": other}],
    )
    wiki_versions = _seed(
        "wiki_versions",
        [{"channel_id": cid}, {"channel_id": other}],
    )
    # wiki_version_counters is keyed by ``_id`` == channel_id, not ``channel_id``.
    wiki_version_counters = _seed(
        "wiki_version_counters",
        [{"_id": cid}, {"_id": other}],
    )
    store._db = _FakeDB(  # type: ignore[attr-defined]
        imported, wiki_versions, wiki_version_counters
    )
    store._activity_events = _seed(  # type: ignore[attr-defined]
        "activity_events",
        [
            {"channel_id": cid, "event_type": "sync_completed"},
            {"channel_id": "global", "details": {"channel_id": cid}},  # nested only
            {"channel_id": other},
        ],
    )
    store._wiki_dirty_queue = _seed(  # type: ignore[attr-defined]
        "wiki_dirty_queue", [{"channel_id": cid}, {"channel_id": other}]
    )
    store._wiki_drift_reports = _seed(  # type: ignore[attr-defined]
        "wiki_drift_reports", [{"channel_id": cid}]
    )
    store._wiki_merge_proposals = _seed(  # type: ignore[attr-defined]
        "wiki_merge_proposals", [{"channel_id": cid}, {"channel_id": cid}]
    )
    store._wiki_proposed_edits = _seed(  # type: ignore[attr-defined]
        "wiki_proposed_edits", []
    )
    store._write_intents = _seed(  # type: ignore[attr-defined]
        "write_intents", [{"channel_id": cid}, {"channel_id": other}]
    )
    store._pipeline_checkpoints = _seed(  # type: ignore[attr-defined]
        "pipeline_checkpoints", [{"channel_id": cid}]
    )
    store._channel_sync_state = _seed(  # type: ignore[attr-defined]
        "channel_sync_state", [{"channel_id": cid}, {"channel_id": other}]
    )
    store._sync_jobs = _seed(  # type: ignore[attr-defined]
        "sync_jobs", [{"channel_id": cid}, {"channel_id": cid}, {"channel_id": other}]
    )
    return store, colls


async def test_purge_channel_returns_per_collection_counts() -> None:
    store, _ = _store_with_fakes()
    counts = await store.purge_channel("C1")

    assert counts["channel_messages"] == 2
    assert counts["imported_messages"] == 1
    assert counts["activity_events"] == 2  # top-level + nested-details rows
    assert counts["wiki_dirty_queue"] == 1
    assert counts["wiki_drift_reports"] == 1
    assert counts["wiki_merge_proposals"] == 2
    assert counts["wiki_proposed_edits"] == 0
    assert counts["wiki_versions"] == 1
    assert counts["wiki_version_counters"] == 1
    assert counts["write_intents"] == 1
    assert counts["pipeline_checkpoints"] == 1
    assert counts["channel_sync_state"] == 1
    assert counts["sync_jobs"] == 2


async def test_purge_channel_leaves_other_channels_intact() -> None:
    store, colls = _store_with_fakes()
    await store.purge_channel("C1")

    # Every collection that had a C2 row still has exactly it.
    assert [d["channel_id"] for d in colls["channel_messages"].docs] == ["C2"]
    assert [d["channel_id"] for d in colls["imported_messages"].docs] == ["C2"]
    assert {d["channel_id"] for d in colls["activity_events"].docs} == {"C2"}
    assert [d["channel_id"] for d in colls["wiki_dirty_queue"].docs] == ["C2"]
    assert [d["channel_id"] for d in colls["wiki_versions"].docs] == ["C2"]
    assert [d["_id"] for d in colls["wiki_version_counters"].docs] == ["C2"]
    assert [d["channel_id"] for d in colls["write_intents"].docs] == ["C2"]
    assert [d["channel_id"] for d in colls["channel_sync_state"].docs] == ["C2"]
    assert [d["channel_id"] for d in colls["sync_jobs"].docs] == ["C2"]


async def test_purge_channel_clears_sync_state_and_jobs() -> None:
    store, colls = _store_with_fakes()
    await store.purge_channel("C1")
    # clear_channel_sync_state removed C1's sync_state + sync_jobs.
    assert all(d["channel_id"] != "C1" for d in colls["channel_sync_state"].docs)
    assert all(d["channel_id"] != "C1" for d in colls["sync_jobs"].docs)


async def test_purge_channel_imported_messages_failure_is_best_effort() -> None:
    store, colls = _store_with_fakes()
    colls["imported_messages"].fail_on_delete = True

    counts = await store.purge_channel("C1")

    # Failure isolated to imported_messages (reported as 0); the rest purged.
    assert counts["imported_messages"] == 0
    assert counts["channel_messages"] == 2
    assert counts["write_intents"] == 1
    assert [d["channel_id"] for d in colls["channel_messages"].docs] == ["C2"]
