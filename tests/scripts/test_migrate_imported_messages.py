"""Tests for ``scripts.migrate_imported_messages_to_channel_messages``.

Covers PR-A.6.2 of the OSS pipeline + wiki redesign — the one-shot migration
from the legacy ``imported_messages`` file-import collection to the unified
``channel_messages`` collection (``source_id="file"``).

No live Mongo — uses lightweight in-memory fakes that mimic the motor
collection surface needed by the script (``find().sort().limit()`` cursor,
``update_one`` for the resume state doc, and the
``upsert_channel_messages`` bulk-write contract from the store).

Convention: no ``@pytest.mark.asyncio`` decorators; ``pyproject.toml`` sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from beever_atlas.scripts import migrate_imported_messages_to_channel_messages as mig
from beever_atlas.stores.mongodb_store import MongoDBStore


# ─────────────────────────────────────────────────────────────────────────────
# Fake motor cursor + collection surface
# ─────────────────────────────────────────────────────────────────────────────


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)
        self._sort_key: str | None = None
        self._sort_dir: int = 1
        self._limit: int | None = None

    def sort(self, key: str, direction: int) -> "_FakeCursor":
        self._sort_key = key
        self._sort_dir = direction
        return self

    def limit(self, n: int) -> "_FakeCursor":
        self._limit = n
        return self

    def __aiter__(self):
        if self._sort_key is not None:
            sort_key = self._sort_key
            self._docs.sort(
                key=lambda d: d.get(sort_key) or 0,
                reverse=(self._sort_dir == -1),
            )
        if self._limit is not None:
            self._docs = self._docs[: self._limit]
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakeImportedMessages:
    """In-memory stand-in for ``stores.mongodb.db['imported_messages']``."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        # Each row gets a synthetic monotonically-increasing _id matching
        # MongoDB ObjectId ordering semantics. Use plain integers — the script
        # only does ``$gt`` comparisons, no ObjectId-specific ops.
        self._docs: list[dict[str, Any]] = []
        for i, row in enumerate(rows, start=1):
            doc = dict(row)
            doc["_id"] = doc.get("_id", i)
            self._docs.append(doc)

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        rows: list[dict[str, Any]] = []
        for doc in self._docs:
            if not self._matches(doc, query):
                continue
            rows.append(dict(doc))
        return _FakeCursor(rows)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if isinstance(v, dict):
                if "$gt" in v and not (doc.get(k) is not None and doc.get(k) > v["$gt"]):
                    return False
            else:
                if doc.get(k) != v:
                    return False
        return True


class _FakeMigrationState:
    """In-memory stand-in for the ``migration_state`` collection."""

    def __init__(self) -> None:
        self._docs: dict[Any, dict[str, Any]] = {}

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        key = query.get("_id")
        doc = self._docs.get(key)
        return dict(doc) if doc is not None else None

    async def update_one(
        self,
        query: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> None:
        key = query["_id"]
        existing = self._docs.get(key, {"_id": key})
        existing = dict(existing)
        for k, v in update.get("$set", {}).items():
            existing[k] = v
        if key not in self._docs:
            for k, v in update.get("$setOnInsert", {}).items():
                existing.setdefault(k, v)
        self._docs[key] = existing


class _FakeBulkResult:
    def __init__(self, inserted: int, modified: int, matched: int, upserted: int) -> None:
        self.inserted_count = inserted
        self.modified_count = modified
        self.matched_count = matched
        self.upserted_ids = {i: object() for i in range(upserted)}


class _FakeChannelMessages:
    """In-memory stand-in for ``channel_messages`` honouring $setOnInsert."""

    def __init__(self) -> None:
        self._docs: dict[tuple[str, str, str], dict[str, Any]] = {}

    @staticmethod
    def _key(filter_: dict[str, Any]) -> tuple[str, str, str]:
        return (filter_["source_id"], filter_["channel_id"], filter_["message_id"])

    async def bulk_write(self, ops: list[Any], ordered: bool = True) -> _FakeBulkResult:
        inserted = 0
        modified = 0
        matched = 0
        upserted = 0
        for op in ops:
            filter_ = op._filter
            update = op._doc
            key = self._key(filter_)
            existing = self._docs.get(key)
            set_part = update.get("$set", {})
            on_insert = update.get("$setOnInsert", {})
            if existing is None:
                self._docs[key] = {**on_insert, **set_part}
                inserted += 1
                upserted += 1
            else:
                matched += 1
                for k, v in set_part.items():
                    if existing.get(k) != v:
                        existing[k] = v
                        modified += 1
                        break
        return _FakeBulkResult(inserted, modified, matched, upserted)


class _FakeDB:
    """``stores.mongodb.db`` lookalike — a dict-of-collections."""

    def __init__(self) -> None:
        self._cols: dict[str, Any] = {}

    def __getitem__(self, name: str) -> Any:
        return self._cols[name]

    def register(self, name: str, col: Any) -> None:
        self._cols[name] = col


# ─────────────────────────────────────────────────────────────────────────────
# Test harness — synthesise a MongoDBStore + DB without touching real Mongo
# ─────────────────────────────────────────────────────────────────────────────


def _make_store(
    imported_rows: list[dict[str, Any]] | None = None,
) -> tuple[MongoDBStore, _FakeImportedMessages, _FakeChannelMessages, _FakeMigrationState]:
    imported = _FakeImportedMessages(imported_rows or [])
    channel_messages = _FakeChannelMessages()
    migration_state = _FakeMigrationState()

    fake_db = _FakeDB()
    fake_db.register("imported_messages", imported)
    fake_db.register("migration_state", migration_state)

    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_messages = channel_messages  # type: ignore[attr-defined]
    store._db = fake_db  # type: ignore[attr-defined]
    # Stub out shutdown — no real client to close.
    store._client = None  # type: ignore[attr-defined]

    async def _noop_shutdown() -> None:  # pragma: no cover — trivially called
        return None

    store.shutdown = _noop_shutdown  # type: ignore[method-assign]
    return store, imported, channel_messages, migration_state


def _imported_row(
    channel_id: str = "ch-1",
    message_id: str = "m1",
    channel_name: str = "Imported Chat",
    content: str = "hello",
    timestamp: datetime | None = None,
) -> dict[str, Any]:
    """Build an ``imported_messages`` row matching ``api/imports.py:561-578``."""
    ts = timestamp or datetime(2026, 4, 1, 12, 0, tzinfo=UTC)
    return {
        "channel_id": channel_id,
        "message_id": message_id,
        "content": content,
        "author": "U_FILE",
        "author_name": "File User",
        "author_image": "",
        "platform": "file",
        "channel_name": channel_name,
        "timestamp": ts,
        "timestamp_iso": ts.isoformat(),
        "thread_id": None,
        "attachments": [],
        "reactions": [],
        "reply_count": 0,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tests — required by the brief
# ─────────────────────────────────────────────────────────────────────────────


async def test_dry_run_does_not_write() -> None:
    """``--dry-run`` produces correct counts but no rows in ``channel_messages``."""
    store, _, channel_messages, migration_state = _make_store(
        [_imported_row(message_id=f"m{i}") for i in range(3)]
    )

    with patch.object(mig, "MongoDBStore", return_value=store):
        summary = await mig.migrate(dry_run=True, batch_size=10)

    assert summary["migrated"] == 3
    assert summary["skipped"] == 0
    assert summary["total_seen"] == 3
    assert summary["dry_run"] is True
    # No writes to channel_messages and no resume-state doc on dry runs.
    assert len(channel_messages._docs) == 0
    assert len(migration_state._docs) == 0


async def test_migration_idempotent() -> None:
    """Running twice with the same input produces the same ``channel_messages`` count."""
    rows = [_imported_row(message_id=f"m{i}") for i in range(5)]
    store, _, channel_messages, _ = _make_store(rows)

    with patch.object(mig, "MongoDBStore", return_value=store):
        first = await mig.migrate(dry_run=False, batch_size=10)
        # Reset the resume cursor so the second run re-scans the source.
        # (Real-world re-runs after a successful run also re-scan and rely on
        # the compound unique index for idempotency.)
        await store.db["migration_state"].update_one(
            {"_id": mig.MIGRATION_STATE_KEY},
            {"$set": {"last_processed_id": None}},
            upsert=True,
        )
        second = await mig.migrate(dry_run=False, batch_size=10)

    assert first["migrated"] == 5
    assert second["migrated"] == 5
    # Crucially: only 5 rows in channel_messages — the compound unique index
    # (modeled by the fake's tuple key) collapses duplicates.
    assert len(channel_messages._docs) == 5


async def test_migration_resumes_from_state_doc() -> None:
    """Partial run + resume picks up where it left off."""
    rows = [_imported_row(message_id=f"m{i}") for i in range(6)]
    store, imported, channel_messages, migration_state = _make_store(rows)

    # Pre-populate the resume cursor as if a prior run finished the first three rows.
    third_id = imported._docs[2]["_id"]
    await migration_state.update_one(
        {"_id": mig.MIGRATION_STATE_KEY},
        {"$set": {"last_processed_id": third_id, "migrated": 3, "skipped": 0}},
        upsert=True,
    )

    with patch.object(mig, "MongoDBStore", return_value=store):
        summary = await mig.migrate(dry_run=False, batch_size=10)

    # Only the remaining 3 rows are migrated this run.
    assert summary["migrated"] == 3
    assert summary["total_seen"] == 3
    # The migrated rows are m3..m5 — proves the cursor was honoured.
    keys = {k for k in channel_messages._docs.keys()}
    assert keys == {("file", "ch-1", f"m{i}") for i in (3, 4, 5)}


async def test_migration_writes_source_id_file() -> None:
    """Every migrated row has ``source_id="file"`` and ``extraction_status="pending"``."""
    rows = [_imported_row(message_id=f"m{i}") for i in range(4)]
    store, _, channel_messages, _ = _make_store(rows)

    with patch.object(mig, "MongoDBStore", return_value=store):
        await mig.migrate(dry_run=False, batch_size=10)

    assert len(channel_messages._docs) == 4
    for doc in channel_messages._docs.values():
        assert doc["source_id"] == "file"
        assert doc["extraction_status"] == "pending"


async def test_migration_preserves_channel_name() -> None:
    """``channel_name`` from ``imported_messages`` lands on ``channel_messages.channel_name``."""
    rows = [
        _imported_row(message_id="m1", channel_name="Slack #general (imported)"),
        _imported_row(message_id="m2", channel_name="Slack #general (imported)"),
    ]
    store, _, channel_messages, _ = _make_store(rows)

    with patch.object(mig, "MongoDBStore", return_value=store):
        await mig.migrate(dry_run=False, batch_size=10)

    for doc in channel_messages._docs.values():
        assert doc["channel_name"] == "Slack #general (imported)"


# ─────────────────────────────────────────────────────────────────────────────
# Extra defensive tests — guard the documented edge cases
# ─────────────────────────────────────────────────────────────────────────────


async def test_migration_skips_rows_without_identity() -> None:
    """Rows missing channel_id or message_id are counted as skipped, not migrated."""
    rows = [
        _imported_row(message_id="m1"),
        {"channel_id": "ch-1", "content": "no id"},
        {"message_id": "m3", "content": "no channel"},
    ]
    store, _, channel_messages, _ = _make_store(rows)

    with patch.object(mig, "MongoDBStore", return_value=store):
        summary = await mig.migrate(dry_run=False, batch_size=10)

    assert summary["migrated"] == 1
    assert summary["skipped"] == 2
    assert summary["total_seen"] == 3
    assert len(channel_messages._docs) == 1


async def test_migration_filters_by_source_channel_id() -> None:
    """``--source-channel-id`` only migrates rows for that channel."""
    rows = [
        _imported_row(channel_id="ch-1", message_id="m1"),
        _imported_row(channel_id="ch-2", message_id="m1"),
        _imported_row(channel_id="ch-1", message_id="m2"),
    ]
    store, _, channel_messages, _ = _make_store(rows)

    with patch.object(mig, "MongoDBStore", return_value=store):
        summary = await mig.migrate(dry_run=False, batch_size=10, source_channel_id="ch-1")

    assert summary["migrated"] == 2
    keys = {k for k in channel_messages._docs.keys()}
    assert keys == {("file", "ch-1", "m1"), ("file", "ch-1", "m2")}
