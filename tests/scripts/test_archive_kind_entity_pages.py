"""Unit tests for archive_kind_entity_pages + drop_archived_kind_entity_pages.

Uses an in-memory fake mongo collection so the script logic can be exercised
without a real Mongo. The fake mirrors the subset of pymongo motor API the
scripts touch: ``find``, ``count_documents``, ``update_many``, ``delete_many``,
``find_one``, ``update_one`` (upsert), and the cursor's ``.limit()`` chain.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest


# --------------------------------------------------------------------------
# Minimal fake mongo collection / database
# --------------------------------------------------------------------------


class _FakeUpdateResult:
    def __init__(self, modified_count: int = 0):
        self.modified_count = modified_count
        self.matched_count = modified_count
        self.deleted_count = modified_count


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]):
        self._docs = list(docs)
        self._limit: int | None = None

    def limit(self, n: int) -> "_FakeCursor":
        self._limit = n
        return self

    def sort(self, *args, **kwargs) -> "_FakeCursor":
        return self

    def __aiter__(self):
        self._iter = iter(self._docs[: self._limit] if self._limit else self._docs)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]] | None = None):
        self.docs: list[dict[str, Any]] = list(docs or [])
        self._next_id = 1

    def _matches(self, doc: dict[str, Any], q: dict[str, Any]) -> bool:
        for k, v in q.items():
            if isinstance(v, dict) and "$ne" in v:
                if doc.get(k) == v["$ne"]:
                    return False
                continue
            if isinstance(v, dict) and "$in" in v:
                if doc.get(k) not in v["$in"]:
                    return False
                continue
            if isinstance(v, dict) and "$lt" in v:
                actual = doc.get(k)
                if actual is None or not (actual < v["$lt"]):
                    return False
                continue
            if doc.get(k) != v:
                return False
        return True

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        results = [d for d in self.docs if self._matches(d, query)]
        return _FakeCursor(results)

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        for d in self.docs:
            if self._matches(d, query):
                return dict(d)
        return None

    async def count_documents(self, query: dict[str, Any]) -> int:
        return sum(1 for d in self.docs if self._matches(d, query))

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> _FakeUpdateResult:
        modified = 0
        sets = (update or {}).get("$set", {})
        for d in self.docs:
            if self._matches(d, query):
                doc_changed = False
                for k, v in sets.items():
                    if d.get(k) != v:
                        d[k] = v
                        doc_changed = True
                    else:
                        # Still record the field write (mirrors mongo's behavior
                        # of writing the value even when unchanged).
                        d[k] = v
                if doc_changed:
                    modified += 1
        return _FakeUpdateResult(modified_count=modified)

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _FakeUpdateResult:
        sets = (update or {}).get("$set", {})
        for d in self.docs:
            if self._matches(d, query):
                d.update(sets)
                return _FakeUpdateResult(modified_count=1)
        if upsert:
            new_doc = {**query, **sets}
            self.docs.append(new_doc)
            return _FakeUpdateResult(modified_count=1)
        return _FakeUpdateResult(modified_count=0)

    async def delete_many(self, query: dict[str, Any]) -> _FakeUpdateResult:
        keep: list[dict[str, Any]] = []
        deleted = 0
        for d in self.docs:
            if self._matches(d, query):
                deleted += 1
            else:
                keep.append(d)
        self.docs = keep
        return _FakeUpdateResult(modified_count=deleted)


class _FakeDB:
    def __init__(self):
        self._coll: dict[str, _FakeCollection] = {
            "wiki_pages": _FakeCollection(),
            "migration_state": _FakeCollection(),
        }

    def __getitem__(self, name: str) -> _FakeCollection:
        return self._coll.setdefault(name, _FakeCollection())


class _FakeMongoStore:
    def __init__(self):
        self._db = _FakeDB()


class _FakeStores:
    def __init__(self):
        self.mongodb = _FakeMongoStore()


def _make_entity_pages(channel_id: str, n: int, archived: bool = False) -> list[dict[str, Any]]:
    return [
        {
            "_id": f"id-{channel_id}-{i}",
            "channel_id": channel_id,
            "kind": "entity",
            "target_lang": "en",
            "page_id": f"entity:{i}",
            "archived": archived,
        }
        for i in range(n)
    ]


# --------------------------------------------------------------------------
# Archive script
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_archive_dry_run_reports_without_writing() -> None:
    from beever_atlas.scripts import archive_kind_entity_pages as script

    fake = _FakeStores()
    fake.mongodb._db["wiki_pages"].docs.extend(_make_entity_pages("C1", 5))

    with patch("beever_atlas.stores.get_stores", lambda: fake):
        stats = await script.archive_kind_entity_pages(
            channel_id="C1",
            batch_size=10,
            dry_run=True,
            unarchive=False,
            target_lang=None,
            resume=False,
        )

    assert stats["matched"] == 5
    # Dry run does NOT mutate the docs.
    for d in fake.mongodb._db["wiki_pages"].docs:
        assert not d.get("archived")


@pytest.mark.asyncio
async def test_archive_idempotent_re_run() -> None:
    from beever_atlas.scripts import archive_kind_entity_pages as script

    fake = _FakeStores()
    fake.mongodb._db["wiki_pages"].docs.extend(_make_entity_pages("C1", 4))

    with patch("beever_atlas.stores.get_stores", lambda: fake):
        stats1 = await script.archive_kind_entity_pages(
            channel_id="C1",
            batch_size=10,
            dry_run=False,
            unarchive=False,
            target_lang=None,
            resume=False,
        )
        stats2 = await script.archive_kind_entity_pages(
            channel_id="C1",
            batch_size=10,
            dry_run=False,
            unarchive=False,
            target_lang=None,
            resume=False,
        )

    assert stats1["modified"] == 4
    # Re-run finds zero rows that need flipping.
    assert stats2["matched"] == 0
    assert stats2["modified"] == 0


@pytest.mark.asyncio
async def test_archive_per_channel_filter() -> None:
    from beever_atlas.scripts import archive_kind_entity_pages as script

    fake = _FakeStores()
    fake.mongodb._db["wiki_pages"].docs.extend(_make_entity_pages("C1", 3))
    fake.mongodb._db["wiki_pages"].docs.extend(_make_entity_pages("C2", 2))

    with patch("beever_atlas.stores.get_stores", lambda: fake):
        stats = await script.archive_kind_entity_pages(
            channel_id="C1",
            batch_size=10,
            dry_run=False,
            unarchive=False,
            target_lang=None,
            resume=False,
        )

    assert stats["matched"] == 3
    # C2 rows untouched.
    c2_archived = [
        d
        for d in fake.mongodb._db["wiki_pages"].docs
        if d.get("channel_id") == "C2" and d.get("archived")
    ]
    assert c2_archived == []


@pytest.mark.asyncio
async def test_archive_unarchive_reverses() -> None:
    from beever_atlas.scripts import archive_kind_entity_pages as script

    fake = _FakeStores()
    fake.mongodb._db["wiki_pages"].docs.extend(_make_entity_pages("C1", 3, archived=True))

    with patch("beever_atlas.stores.get_stores", lambda: fake):
        stats = await script.archive_kind_entity_pages(
            channel_id="C1",
            batch_size=10,
            dry_run=False,
            unarchive=True,
            target_lang=None,
            resume=False,
        )

    assert stats["modified"] == 3
    for d in fake.mongodb._db["wiki_pages"].docs:
        assert not d.get("archived")


# --------------------------------------------------------------------------
# Drop script
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drop_dry_run_reports_without_deleting() -> None:
    from beever_atlas.scripts import drop_archived_kind_entity_pages as script

    fake = _FakeStores()
    old_ts = (datetime.now(tz=UTC) - timedelta(days=60)).isoformat()
    docs = _make_entity_pages("C1", 3, archived=True)
    for d in docs:
        d["archived_at"] = old_ts
    fake.mongodb._db["wiki_pages"].docs.extend(docs)

    with patch("beever_atlas.stores.get_stores", lambda: fake):
        stats = await script.drop_archived_kind_entity_pages(
            channel_id="C1",
            batch_size=10,
            dry_run=True,
            min_archived_age_days=30,
            target_lang=None,
        )

    assert stats["matched"] == 3
    assert stats["deleted"] == 0
    # Rows still present.
    assert len(fake.mongodb._db["wiki_pages"].docs) == 3


@pytest.mark.asyncio
async def test_drop_main_refuses_without_confirm() -> None:
    from beever_atlas.scripts import drop_archived_kind_entity_pages as script

    rc = script.main(["--channel-id", "C1"])  # no --confirm, no --dry-run
    assert rc == 2


@pytest.mark.asyncio
async def test_drop_respects_min_archived_age_days() -> None:
    from beever_atlas.scripts import drop_archived_kind_entity_pages as script

    fake = _FakeStores()
    # Mix of old + new archived rows.
    new_ts = (datetime.now(tz=UTC) - timedelta(days=5)).isoformat()
    old_ts = (datetime.now(tz=UTC) - timedelta(days=60)).isoformat()
    new_docs = _make_entity_pages("C1", 2, archived=True)
    for d in new_docs:
        d["archived_at"] = new_ts
    old_docs = _make_entity_pages("C2", 3, archived=True)
    for d in old_docs:
        d["archived_at"] = old_ts
    fake.mongodb._db["wiki_pages"].docs.extend(new_docs + old_docs)

    with patch("beever_atlas.stores.get_stores", lambda: fake):
        stats = await script.drop_archived_kind_entity_pages(
            channel_id=None,
            batch_size=10,
            dry_run=False,
            min_archived_age_days=30,
            target_lang=None,
        )

    # Only the 60-day-old C2 rows are deleted.
    assert stats["deleted"] == 3
    remaining = [d for d in fake.mongodb._db["wiki_pages"].docs]
    assert all(d.get("channel_id") == "C1" for d in remaining)
