"""Tests for the wiki-pages slug-identity migration script (§8.6).

Uses a lightweight in-memory fake mongo client so the test exercises
the actual ``migrate(...)`` function without requiring a live Mongo.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Fake Mongo collection — supports the operators the migrator uses
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
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if isinstance(v, dict):
                if "$gt" in v and not (doc.get(k) is not None and doc.get(k) > v["$gt"]):
                    return False
            elif doc.get(k) != v:
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


def _seed_pages(coll: _FakeCollection, pages: list[dict[str, Any]]) -> None:
    coll.docs = [dict(p) for p in pages]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_dry_run_plans_changes_but_writes_nothing() -> None:
    coll = _FakeCollection()
    _seed_pages(
        coll,
        [
            {
                "channel_id": "C1",
                "target_lang": "en",
                "page_id": f"topic:item-{i}",
                "title": f"Item {i}",
                "slug": "",  # empty → migration plans an assignment
                "kind": "topic",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            for i in range(25)
        ],
    )
    with patch(
        "scripts.migrate_wiki_pages_to_slug_identity.AsyncIOMotorClient",
        return_value=_FakeClient(coll),
    ):
        from scripts.migrate_wiki_pages_to_slug_identity import migrate

        counters = await migrate(
            mongodb_uri="mongodb://fake",
            channel_id=None,
            dry_run=True,
        )
    assert counters["planned"] == 25
    assert counters["written"] == 0
    assert counters["skipped"] == 0
    assert coll.update_calls == []


async def test_real_run_writes_planned_changes_then_skips_on_replay() -> None:
    coll = _FakeCollection()
    _seed_pages(
        coll,
        [
            {
                "channel_id": "C1",
                "target_lang": "en",
                "page_id": f"topic:item-{i}",
                "title": f"Item {i}",
                "slug": "",
                "kind": "topic",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            }
            for i in range(25)
        ],
    )

    async def _run(dry_run: bool) -> dict[str, int]:
        with patch(
            "scripts.migrate_wiki_pages_to_slug_identity.AsyncIOMotorClient",
            return_value=_FakeClient(coll),
        ):
            from scripts.migrate_wiki_pages_to_slug_identity import migrate

            return await migrate(
                mongodb_uri="mongodb://fake",
                channel_id=None,
                dry_run=dry_run,
            )

    first = await _run(dry_run=False)
    assert first["planned"] == 25
    assert first["written"] == 25

    # Re-run is a no-op — every page now has a slug, so the planner
    # skips them.
    second = await _run(dry_run=False)
    assert second["planned"] == 0
    assert second["written"] == 0
    assert second["skipped"] == 25


async def test_dedupe_collisions_via_suffix() -> None:
    coll = _FakeCollection()
    _seed_pages(
        coll,
        [
            {
                "channel_id": "C1",
                "target_lang": "en",
                "page_id": "topic:auth",
                "title": "Authentication",
                "slug": "",
                "kind": "topic",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
            {
                "channel_id": "C1",
                "target_lang": "en",
                "page_id": "topic:auth-2",
                "title": "Authentication",  # same title → collision
                "slug": "",
                "kind": "topic",
                "created_at": datetime(2026, 1, 2, tzinfo=UTC),
            },
        ],
    )
    with patch(
        "scripts.migrate_wiki_pages_to_slug_identity.AsyncIOMotorClient",
        return_value=_FakeClient(coll),
    ):
        from scripts.migrate_wiki_pages_to_slug_identity import migrate

        await migrate(mongodb_uri="mongodb://fake", channel_id=None, dry_run=False)
    slugs = {d["slug"] for d in coll.docs}
    # Older row wins the bare slug; newer row gets the -2 suffix.
    assert "authentication" in slugs
    assert "authentication-2" in slugs


async def test_derives_kind_from_page_id_when_default() -> None:
    """Legacy entity / decisions / faq / action-items pages with the
    model-default ``kind="topic"`` get the structural derivation
    written; legitimate ``topic`` pages stay topic."""
    coll = _FakeCollection()
    _seed_pages(
        coll,
        [
            {
                "channel_id": "C1",
                "target_lang": "en",
                "page_id": "entity:alice",
                "title": "Alice",
                "slug": "entity-alice",  # already has slug
                "kind": "topic",  # default — should become "entity"
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
            {
                "channel_id": "C1",
                "target_lang": "en",
                "page_id": "topic:auth",
                "title": "Authentication",
                "slug": "topic-auth",
                "kind": "topic",  # correct — should NOT be touched
                "created_at": datetime(2026, 1, 2, tzinfo=UTC),
            },
        ],
    )
    with patch(
        "scripts.migrate_wiki_pages_to_slug_identity.AsyncIOMotorClient",
        return_value=_FakeClient(coll),
    ):
        from scripts.migrate_wiki_pages_to_slug_identity import migrate

        counters = await migrate(mongodb_uri="mongodb://fake", channel_id=None, dry_run=False)
    assert counters["written"] == 1
    by_id = {d["page_id"]: d for d in coll.docs}
    assert by_id["entity:alice"]["kind"] == "entity"
    assert by_id["topic:auth"]["kind"] == "topic"


async def test_channel_id_filter_scopes_migration() -> None:
    coll = _FakeCollection()
    _seed_pages(
        coll,
        [
            {
                "channel_id": "C1",
                "target_lang": "en",
                "page_id": "topic:a",
                "title": "A",
                "slug": "",
                "kind": "topic",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
            {
                "channel_id": "C2",
                "target_lang": "en",
                "page_id": "topic:b",
                "title": "B",
                "slug": "",
                "kind": "topic",
                "created_at": datetime(2026, 1, 1, tzinfo=UTC),
            },
        ],
    )
    with patch(
        "scripts.migrate_wiki_pages_to_slug_identity.AsyncIOMotorClient",
        return_value=_FakeClient(coll),
    ):
        from scripts.migrate_wiki_pages_to_slug_identity import migrate

        counters = await migrate(mongodb_uri="mongodb://fake", channel_id="C1", dry_run=False)
    assert counters["written"] == 1
    by_id = {d["page_id"]: d for d in coll.docs}
    assert by_id["topic:a"]["slug"] == "a"
    # C2's page is untouched.
    assert by_id["topic:b"]["slug"] == ""
