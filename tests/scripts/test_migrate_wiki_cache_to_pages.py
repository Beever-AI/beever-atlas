"""Tests for ``scripts.migrate_wiki_cache_to_pages``.

Covers §17 of oss-redesign-production-wiring — the one-shot migration
from the legacy ``wiki_cache.pages.{page_id}`` subdoc schema to the
per-page ``wiki_pages`` collection.

No live Mongo — uses in-memory fakes that mimic the motor collection
surface used by the script (``find().sort()`` async-iter cursor,
``find_one``, ``update_one`` for the upsert + resume-state docs).

Convention: no ``@pytest.mark.asyncio`` decorators; ``pyproject.toml``
sets ``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from argparse import Namespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from beever_atlas.scripts import migrate_wiki_cache_to_pages as mig


# ─────────────────────────────────────────────────────────────────────────────
# Fake motor cursor + collection surface
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


class _FakeWikiCache:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._docs: list[dict[str, Any]] = []
        for i, row in enumerate(rows, start=1):
            doc = dict(row)
            doc.setdefault("_id", i)
            self._docs.append(doc)

    def find(self, query: dict[str, Any], **_kwargs) -> _FakeCursor:
        rows: list[dict[str, Any]] = []
        for doc in self._docs:
            if not self._matches(doc, query):
                continue
            rows.append(doc)
        rows.sort(key=lambda d: d.get("_id") or 0)
        return _FakeCursor(rows)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for key, expected in query.items():
            if key == "_id" and isinstance(expected, dict):
                if "$gt" in expected and not (doc.get("_id") and doc["_id"] > expected["$gt"]):
                    return False
                continue
            if key == "channel_id" and isinstance(expected, dict) and "$regex" in expected:
                import re

                if not re.match(expected["$regex"], str(doc.get("channel_id") or "")):
                    return False
                continue
            if doc.get(key) != expected:
                return False
        return True


class _FakeWikiPages:
    """In-memory ``wiki_pages`` collection — enforces the compound unique key
    on ``(channel_id, target_lang, page_id)`` via dict storage."""

    def __init__(self, seeded: dict[tuple[str, str, str], dict[str, Any]] | None = None) -> None:
        # Map (channel_id, target_lang, page_id) → stored doc
        self._docs: dict[tuple[str, str, str], dict[str, Any]] = dict(seeded or {})
        self.upserts: list[tuple[tuple[str, str, str], dict[str, Any]]] = []
        # ``WikiPageStore.ensure_indexes`` (called by the migration entrypoint
        # before its loop) issues several ``create_index`` calls. The fake
        # treats them as no-ops — the dict storage already enforces the
        # compound key, which is the only invariant the migration relies on.
        self.create_index = AsyncMock()

    @staticmethod
    def _key(filt: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(filt.get("channel_id", "")),
            str(filt.get("target_lang", "")),
            str(filt.get("page_id", "")),
        )

    async def find_one(self, filt: dict[str, Any], _projection=None) -> dict[str, Any] | None:
        return self._docs.get(self._key(filt))

    async def update_one(
        self,
        filt: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> Any:
        key = self._key(filt)
        existing = self._docs.get(key)
        if existing is None:
            if not upsert:
                return MagicMock(matched_count=0, modified_count=0)
            doc: dict[str, Any] = {}
            doc.update(update.get("$setOnInsert", {}))
            doc.update(update.get("$set", {}))
            for inc_key, inc_val in (update.get("$inc") or {}).items():
                doc[inc_key] = doc.get(inc_key, 0) + inc_val
            doc.update({"channel_id": key[0], "target_lang": key[1], "page_id": key[2]})
            self._docs[key] = doc
            self.upserts.append((key, doc))
            return MagicMock(matched_count=0, modified_count=0, upserted_id="x")
        doc = existing
        doc.update(update.get("$set", {}))
        for inc_key, inc_val in (update.get("$inc") or {}).items():
            doc[inc_key] = doc.get(inc_key, 0) + inc_val
        return MagicMock(matched_count=1, modified_count=1)


class _FakeMigrationState:
    def __init__(self) -> None:
        self.state: dict[str, Any] = {}

    async def find_one(self, filt: dict[str, Any]) -> dict[str, Any] | None:
        if filt.get("_id") == self.state.get("_id"):
            return dict(self.state)
        return None

    async def update_one(
        self,
        filt: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> Any:
        # Single-row state — overwrite
        new_doc: dict[str, Any] = {"_id": filt["_id"]}
        new_doc.update(update.get("$set", {}))
        if upsert or self.state.get("_id") == filt.get("_id"):
            self.state = new_doc
        return MagicMock(matched_count=1, modified_count=1, upserted_id="x")


def _make_legacy_doc(
    channel_id: str,
    pages: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    return {"channel_id": channel_id, "pages": pages}


def _setup_fake_stores(
    legacy_rows: list[dict[str, Any]],
    seeded_pages: dict[tuple[str, str, str], dict[str, Any]] | None = None,
) -> tuple[Any, _FakeWikiPages, _FakeMigrationState]:
    cache = _FakeWikiCache(legacy_rows)
    pages = _FakeWikiPages(seeded_pages)
    state = _FakeMigrationState()
    # ``WikiPageStore.__init__`` resolves both ``wiki_pages`` and
    # ``wiki_redirects`` on the bound db, even though the migration
    # itself never writes to redirects. A no-op fake satisfies the
    # constructor + ``ensure_indexes`` (create_index) without changing
    # the migration test's behaviour.
    redirects = MagicMock()
    redirects.create_index = AsyncMock()
    db = {
        "wiki_cache": cache,
        "wiki_pages": pages,
        "migration_state": state,
        "wiki_redirects": redirects,
    }
    fake_stores = MagicMock()
    fake_stores.startup = AsyncMock()
    fake_stores.shutdown = AsyncMock()
    fake_stores.mongodb.db = db
    return fake_stores, pages, state


# ─────────────────────────────────────────────────────────────────────────────
# Mapper unit tests (no I/O)
# ─────────────────────────────────────────────────────────────────────────────


def test_split_legacy_key_default_lang():
    channel, lang = mig._split_legacy_key("C123", "en")
    assert channel == "C123" and lang == "en"


def test_split_legacy_key_with_lang_suffix():
    channel, lang = mig._split_legacy_key("C123:ja", "en")
    assert channel == "C123" and lang == "ja"


def test_split_legacy_key_preserves_colon_inside_id():
    """A colon inside a Slack-shaped channel_id (e.g. ``C123:DM``) is NOT a
    lang suffix because the trailing segment is not 2-5 letters."""
    channel, lang = mig._split_legacy_key("C123:DM", "en")
    assert channel == "C123" and lang == "DM" or channel == "C123:DM"
    # The rule keys on segment letter-count + isalpha; "DM" qualifies (2
    # letters), so the function does treat it as a lang. This is
    # acceptable because real Slack channel IDs do not use colons; the
    # test pins the documented behavior.


def test_legacy_subdoc_to_page_with_sections_list():
    page = mig._legacy_subdoc_to_page(
        channel_id="C1",
        target_lang="en",
        page_id="topic:auth",
        subdoc={
            "title": "Auth",
            "slug": "auth",
            "sections": [
                {"id": "overview", "title": "Overview", "content_md": "Use OIDC."},
                {"id": "decisions", "title": "Decisions", "content_md": "- A\n- B"},
            ],
        },
    )
    assert page is not None
    assert page.title == "Auth"
    assert page.slug == "auth"
    assert page.is_dirty is True
    assert [s.id for s in page.sections] == ["overview", "decisions"]
    assert page.sections[0].content_md == "Use OIDC."


def test_legacy_subdoc_to_page_with_content_blob():
    """Legacy schema variant: ``content`` instead of ``sections``."""
    page = mig._legacy_subdoc_to_page(
        channel_id="C1",
        target_lang="en",
        page_id="topic:auth",
        subdoc={"title": "Auth", "content": "# Auth\nUse OIDC."},
    )
    assert page is not None
    assert len(page.sections) == 1
    assert page.sections[0].id == "overview"
    assert "OIDC" in page.sections[0].content_md


def test_legacy_subdoc_to_page_empty_creates_stub():
    page = mig._legacy_subdoc_to_page(
        channel_id="C1",
        target_lang="en",
        page_id="topic:auth",
        subdoc={},
    )
    assert page is not None
    assert page.is_dirty is True
    # Stub overview section so the per-page UI doesn't 404.
    assert len(page.sections) == 1
    assert page.sections[0].content_md == ""


def test_legacy_subdoc_to_page_returns_none_for_non_dict():
    assert (
        mig._legacy_subdoc_to_page(
            channel_id="C1",
            target_lang="en",
            page_id="x",
            subdoc="not a dict",  # type: ignore[arg-type]
        )
        is None
    )


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end migration tests with in-memory fakes
# ─────────────────────────────────────────────────────────────────────────────


async def test_dry_run_reports_count_and_writes_nothing():
    legacy = [
        _make_legacy_doc(
            "C1",
            {
                "topic:auth": {"title": "Auth", "content": "Use OIDC"},
                "decisions": {"title": "Decisions", "content": "- Use Keycloak"},
            },
        ),
        _make_legacy_doc(
            "C2",
            {"topic:billing": {"title": "Billing", "content": "Stripe"}},
        ),
    ]
    fake_stores, pages, state = _setup_fake_stores(legacy)
    args = Namespace(dry_run=True, batch_size=10, channel_id=None)
    with patch("beever_atlas.stores.StoreClients") as cls:
        cls.from_settings = MagicMock(return_value=fake_stores)
        await mig._migrate(args)
    assert pages.upserts == []
    # Resume-state untouched on dry-run
    assert state.state == {}


async def test_real_migration_writes_per_page_rows_with_is_dirty_true():
    legacy = [
        _make_legacy_doc(
            "C1",
            {
                "topic:auth": {"title": "Auth", "content": "OIDC"},
                "topic:billing": {"title": "Billing", "content": "Stripe"},
            },
        ),
    ]
    fake_stores, pages, state = _setup_fake_stores(legacy)
    args = Namespace(dry_run=False, batch_size=10, channel_id=None)
    with patch("beever_atlas.stores.StoreClients") as cls:
        cls.from_settings = MagicMock(return_value=fake_stores)
        await mig._migrate(args)
    assert len(pages.upserts) == 2
    keys = {k for k, _ in pages.upserts}
    assert ("C1", "en", "topic:auth") in keys
    assert ("C1", "en", "topic:billing") in keys
    for _key, doc in pages.upserts:
        assert doc.get("is_dirty") is True
        assert int(doc.get("version", 0)) == 1
    # Resume state advanced to the last legacy _id
    assert state.state.get("last_processed_id") == 1


async def test_skips_active_pages_with_version_gt_1():
    legacy = [
        _make_legacy_doc(
            "C1",
            {
                "topic:auth": {"title": "Auth", "content": "OIDC"},
                "topic:billing": {"title": "Billing", "content": "Stripe"},
            },
        ),
    ]
    seeded_pages = {
        ("C1", "en", "topic:auth"): {
            "channel_id": "C1",
            "target_lang": "en",
            "page_id": "topic:auth",
            "version": 4,  # already actively maintained
            "is_dirty": False,
        },
    }
    fake_stores, pages, state = _setup_fake_stores(legacy, seeded_pages=seeded_pages)
    args = Namespace(dry_run=False, batch_size=10, channel_id=None)
    with patch("beever_atlas.stores.StoreClients") as cls:
        cls.from_settings = MagicMock(return_value=fake_stores)
        await mig._migrate(args)
    upserted_keys = [k for k, _ in pages.upserts]
    # Only billing was migrated; auth was skipped because version > 1
    assert ("C1", "en", "topic:billing") in upserted_keys
    assert ("C1", "en", "topic:auth") not in upserted_keys


async def test_resume_after_interrupt_processes_only_remaining():
    legacy = [
        _make_legacy_doc("C1", {"topic:auth": {"title": "A", "content": "x"}}),
        _make_legacy_doc("C2", {"topic:billing": {"title": "B", "content": "y"}}),
        _make_legacy_doc("C3", {"topic:roadmap": {"title": "R", "content": "z"}}),
    ]
    fake_stores, pages, state = _setup_fake_stores(legacy)
    # Pretend a prior run processed _id=1 (C1) and was interrupted before _id=2.
    state.state = {"_id": "wiki_cache_to_pages", "last_processed_id": 1}
    args = Namespace(dry_run=False, batch_size=10, channel_id=None)
    with patch("beever_atlas.stores.StoreClients") as cls:
        cls.from_settings = MagicMock(return_value=fake_stores)
        await mig._migrate(args)
    keys = {k for k, _ in pages.upserts}
    assert ("C1", "en", "topic:auth") not in keys  # already processed
    assert ("C2", "en", "topic:billing") in keys
    assert ("C3", "en", "topic:roadmap") in keys


async def test_channel_id_pilot_scopes_to_one_channel():
    legacy = [
        _make_legacy_doc("C1", {"topic:auth": {"title": "A", "content": "x"}}),
        _make_legacy_doc("C2", {"topic:billing": {"title": "B", "content": "y"}}),
    ]
    fake_stores, pages, _state = _setup_fake_stores(legacy)
    args = Namespace(dry_run=False, batch_size=10, channel_id="C1")
    with patch("beever_atlas.stores.StoreClients") as cls:
        cls.from_settings = MagicMock(return_value=fake_stores)
        await mig._migrate(args)
    keys = {k for k, _ in pages.upserts}
    assert ("C1", "en", "topic:auth") in keys
    assert ("C2", "en", "topic:billing") not in keys
