"""Unit tests for the per-page wiki document store (PR-E).

Tests use a lightweight in-memory fake collection — no live Mongo
container required. Mirrors the pattern used by
``tests/stores/test_channel_messages_store.py``.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/wiki-page-store/``
"""

from __future__ import annotations

from typing import Any

from beever_atlas.models.persistence import (
    WikiPage,
    WikiPageSection,
    WikiTension,
)
from beever_atlas.wiki.page_store import WikiPageStore


# ─────────────────────────────────────────────────────────────────────────────
# Fake Mongo collection — supports only the operators WikiPageStore uses
# ─────────────────────────────────────────────────────────────────────────────


class _FakeUpdateResult:
    def __init__(self, modified: int) -> None:
        self.modified_count = modified


class _FakeDeleteResult:
    def __init__(self, deleted: int) -> None:
        self.deleted_count = deleted


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)
        self._sort_key: str | None = None
        self._sort_dir: int = 1

    def sort(self, key: str, direction: int) -> "_FakeCursor":
        self._sort_key = key
        self._sort_dir = direction
        return self

    def __aiter__(self):
        if self._sort_key:
            self._docs.sort(
                key=lambda d: d.get(self._sort_key) or "",
                reverse=(self._sort_dir == -1),
            )
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._docs:
            raise StopAsyncIteration
        return self._docs.pop(0)


class _FakeWikiPagesCollection:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str, str], dict[str, Any]] = {}
        # Captures every ``create_index`` call so introspection tests can
        # assert the redesign indexes were requested with the right keys.
        self.index_calls: list[dict[str, Any]] = []

    async def create_index(self, keys, **kwargs) -> None:
        self.index_calls.append({"keys": keys, **kwargs})

    @staticmethod
    def _key(query: dict[str, Any]) -> tuple[str, str, str]:
        return (query["channel_id"], query["target_lang"], query["page_id"])

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if isinstance(v, dict):
                if "$in" in v and doc.get(k) not in v["$in"]:
                    return False
            elif doc.get(k) != v:
                return False
        return True

    async def find_one(
        self, query: dict[str, Any], projection: dict[str, int] | None = None
    ) -> dict[str, Any] | None:
        if (
            "channel_id" in query
            and "target_lang" in query
            and "page_id" in query
            and not isinstance(query["page_id"], dict)
        ):
            doc = self.docs.get(self._key(query))
            return dict(doc) if doc else None
        for doc in self.docs.values():
            if self._matches(doc, query):
                return dict(doc)
        return None

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        rows = [dict(doc) for doc in self.docs.values() if self._matches(doc, query)]
        return _FakeCursor(rows)

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _FakeUpdateResult:
        if (
            "channel_id" in query
            and "target_lang" in query
            and "page_id" in query
            and not isinstance(query["page_id"], dict)
        ):
            key = self._key(query)
            existing = self.docs.get(key)
            is_insert = existing is None
            if is_insert and not upsert:
                return _FakeUpdateResult(0)
            if is_insert:
                self.docs[key] = {}
                existing = self.docs[key]
            # Apply $setOnInsert FIRST so $set can override on update.
            if is_insert:
                for k, v in update.get("$setOnInsert", {}).items():
                    existing[k] = v
            for k, v in update.get("$set", {}).items():
                existing[k] = v
            # $inc — atomic counter bump (PR-E close-out: WikiPageStore
            # uses this to advance ``version`` without a read-modify-write
            # race).
            for k, v in update.get("$inc", {}).items():
                existing[k] = (existing.get(k) or 0) + v
            for k, v in update.get("$push", {}).items():
                if isinstance(v, dict) and "$each" in v:
                    existing.setdefault(k, []).extend(v["$each"])
                else:
                    existing.setdefault(k, []).append(v)
            return _FakeUpdateResult(1)
        return _FakeUpdateResult(0)

    async def update_many(self, query: dict[str, Any], update: dict[str, Any]) -> _FakeUpdateResult:
        modified = 0
        for doc in self.docs.values():
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                modified += 1
        return _FakeUpdateResult(modified)

    async def delete_one(self, query: dict[str, Any]) -> _FakeDeleteResult:
        if "channel_id" in query and "target_lang" in query and "page_id" in query:
            key = self._key(query)
            if key in self.docs:
                del self.docs[key]
                return _FakeDeleteResult(1)
        return _FakeDeleteResult(0)


def _make_store() -> tuple[WikiPageStore, _FakeWikiPagesCollection]:
    fake = _FakeWikiPagesCollection()
    store = WikiPageStore.__new__(WikiPageStore)
    store._db = None  # type: ignore[attr-defined]
    store._collection = fake  # type: ignore[attr-defined]
    return store, fake


def _make_page(
    *,
    channel_id: str = "C1",
    page_id: str = "topic:auth",
    target_lang: str = "en",
    title: str = "Auth",
) -> WikiPage:
    return WikiPage(
        channel_id=channel_id,
        target_lang=target_lang,
        page_id=page_id,
        title=title,
        slug=page_id.replace(":", "-"),
        sections=[
            WikiPageSection(id="overview", title="Overview", content_md="# Auth"),
            WikiPageSection(id="decisions", title="Decisions", content_md=""),
        ],
    )


# ─────────────────────────────────────────────────────────────────────────────
# get_page / save_page
# ─────────────────────────────────────────────────────────────────────────────


async def test_get_page_returns_none_when_missing() -> None:
    store, _ = _make_store()
    result = await store.get_page("C1", "missing")
    assert result is None


async def test_save_page_then_get_round_trip() -> None:
    store, _ = _make_store()
    page = _make_page()
    await store.save_page(page)
    fetched = await store.get_page("C1", "topic:auth")
    assert fetched is not None
    assert fetched.title == "Auth"
    assert len(fetched.sections) == 2
    assert fetched.version == 1  # bumped from 0 → 1 on first save


async def test_save_page_bumps_version_on_each_content_change() -> None:
    """Version increments on each save where the user-visible content
    differs from the prior persisted doc. Identical-content saves are
    suppressed (see ``test_save_page_no_bump_when_content_unchanged``)."""
    store, _ = _make_store()
    # Save 1 — first insert (version 0 → 1).
    await store.save_page(_make_page(title="Auth v1"))
    # Save 2 — title diff (1 → 2).
    await store.save_page(_make_page(title="Auth v2"))
    # Save 3 — title diff (2 → 3).
    await store.save_page(_make_page(title="Auth v3"))
    fetched = await store.get_page("C1", "topic:auth")
    assert fetched is not None
    assert fetched.version == 3


async def test_save_page_no_bump_when_content_unchanged() -> None:
    """H-3 / H-4: when the new save is byte-identical to the prior
    persisted doc on the user-visible content fields (title, slug,
    sections, modules, narrative_sections, …), the version counter is
    NOT incremented. ``updated_at`` is still refreshed so list-by-
    recency ordering reflects the touch.
    """
    store, _ = _make_store()
    # First save — fresh insert, version 0 → 1.
    await store.save_page(_make_page())
    first = await store.get_page("C1", "topic:auth")
    assert first is not None and first.version == 1
    # Second save — identical content, version must stay at 1.
    await store.save_page(_make_page())
    second = await store.get_page("C1", "topic:auth")
    assert second is not None
    assert second.version == 1
    # Third save — still identical, still 1.
    await store.save_page(_make_page())
    third = await store.get_page("C1", "topic:auth")
    assert third is not None
    assert third.version == 1


async def test_save_page_no_bump_with_identical_narrative_sections() -> None:
    """H-3 / H-4 narrative-specific case: when ``narrative_sections``
    matches the prior persisted doc, version stays put. Re-running the
    compiler on the same fact set deterministically produces the same
    article — so unchanged narrative should not drift the version."""
    store, _ = _make_store()
    section = {
        "anchor": "context",
        "heading": "Context",
        "paragraphs": [
            {"text": "Authlib was adopted.", "citations": ["f_1"], "is_inference": False}
        ],
        "citations": ["f_1"],
        "visual": None,
        "citation_coverage": 1.0,
    }
    page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="topic:auth",
        title="Auth",
        slug="topic-auth",
        sections=[WikiPageSection(id="overview", title="Overview", content_md="# Auth")],
        narrative_sections=[section],
    )
    await store.save_page(page)
    after_first = await store.get_page("C1", "topic:auth")
    assert after_first is not None
    assert after_first.version == 1
    assert after_first.narrative_sections == [section]
    # Save the same page again — narrative bytes match, version frozen.
    await store.save_page(page)
    after_second = await store.get_page("C1", "topic:auth")
    assert after_second is not None
    assert after_second.version == 1
    # Modify the narrative — version bumps.
    new_section = {**section, "heading": "Background"}
    page.narrative_sections = [new_section]
    await store.save_page(page)
    after_third = await store.get_page("C1", "topic:auth")
    assert after_third is not None
    assert after_third.version == 2
    assert after_third.narrative_sections[0]["heading"] == "Background"


async def test_save_page_preserves_created_at_across_saves() -> None:
    """Code-review MEDIUM (second pass): ``created_at`` must NOT be
    overwritten on subsequent saves.

    The maintainer (PR-F) typically re-builds a fresh ``WikiPage``
    instance on each refresh; Pydantic's ``default_factory`` for
    ``created_at`` would set a new ``datetime.now()`` every time. If
    save_page put ``created_at`` in ``$set``, the original creation
    timestamp would drift forward on every save and audit queries
    ('how old is this page?') would lie.
    """
    store, fake = _make_store()
    await store.save_page(_make_page())
    first = await store.get_page("C1", "topic:auth")
    assert first is not None
    original_created_at = first.created_at

    # Second save with a freshly-instantiated WikiPage — its
    # default_factory will populate a NEW created_at.
    await store.save_page(_make_page())
    second = await store.get_page("C1", "topic:auth")
    assert second is not None
    assert second.created_at == original_created_at, (
        "created_at must be preserved across saves — $setOnInsert regression"
    )


async def test_save_page_uses_atomic_inc_for_version() -> None:
    """Code-review HIGH regression: ``save_page`` must use ``$inc`` for the
    version bump rather than a read-then-write pattern. We verify by
    inspecting the actual update operator the store sends to the
    collection — if the implementation regressed to ``$set: {version: N+1}``,
    two concurrent writers would both read N and both write N+1.

    This test guards the contract structurally — the in-memory fake's
    ``$inc`` handler does the right thing only because the production
    code requested ``$inc``.
    """
    captured_updates: list[dict[str, Any]] = []

    class _Capturer:
        def __init__(self, inner):
            self.inner = inner
            self.docs = inner.docs

        async def update_one(self, query, update, upsert=False):
            captured_updates.append(update)
            return await self.inner.update_one(query, update, upsert=upsert)

        def __getattr__(self, name):
            return getattr(self.inner, name)

    fake = _FakeWikiPagesCollection()
    capturer = _Capturer(fake)
    store = WikiPageStore.__new__(WikiPageStore)
    store._db = None  # type: ignore[attr-defined]
    store._collection = capturer  # type: ignore[attr-defined]

    await store.save_page(_make_page())
    assert len(captured_updates) == 1
    update = captured_updates[0]
    assert "$inc" in update, (
        "save_page must use $inc for version, not $set — otherwise concurrent writers race"
    )
    assert update["$inc"] == {"version": 1}
    # And $set MUST NOT carry version (would override the atomic $inc).
    assert "version" not in update.get("$set", {}), (
        "version MUST NOT appear in $set — $inc is the only writer"
    )


async def test_save_page_isolates_channels() -> None:
    """Two channels with the same page_id are independent rows."""
    store, _ = _make_store()
    a = _make_page(channel_id="A", page_id="overview")
    b = _make_page(channel_id="B", page_id="overview")
    await store.save_page(a)
    await store.save_page(b)
    fetched_a = await store.get_page("A", "overview")
    fetched_b = await store.get_page("B", "overview")
    assert fetched_a is not None and fetched_a.channel_id == "A"
    assert fetched_b is not None and fetched_b.channel_id == "B"


async def test_save_page_isolates_target_langs() -> None:
    """English and Chinese pages don't collide on the same page_id."""
    store, _ = _make_store()
    en = _make_page(target_lang="en", title="Auth")
    zh = _make_page(target_lang="zh-HK", title="認證")
    await store.save_page(en)
    await store.save_page(zh)
    fetched_en = await store.get_page("C1", "topic:auth", target_lang="en")
    fetched_zh = await store.get_page("C1", "topic:auth", target_lang="zh-HK")
    assert fetched_en is not None and fetched_en.title == "Auth"
    assert fetched_zh is not None and fetched_zh.title == "認證"


# ─────────────────────────────────────────────────────────────────────────────
# list_pages
# ─────────────────────────────────────────────────────────────────────────────


async def test_list_pages_returns_all_for_channel() -> None:
    store, _ = _make_store()
    await store.save_page(_make_page(page_id="a"))
    await store.save_page(_make_page(page_id="b"))
    await store.save_page(_make_page(page_id="c"))
    pages = await store.list_pages("C1")
    assert {p.page_id for p in pages} == {"a", "b", "c"}


async def test_list_pages_does_not_leak_other_channels() -> None:
    store, _ = _make_store()
    await store.save_page(_make_page(channel_id="A", page_id="hidden"))
    await store.save_page(_make_page(channel_id="B", page_id="visible"))
    pages = await store.list_pages("B")
    assert {p.page_id for p in pages} == {"visible"}


# ─────────────────────────────────────────────────────────────────────────────
# mark_dirty / clear_dirty (manual mode for PR-F)
# ─────────────────────────────────────────────────────────────────────────────


async def test_mark_dirty_sets_is_dirty_on_named_pages() -> None:
    store, fake = _make_store()
    await store.save_page(_make_page(page_id="a"))
    await store.save_page(_make_page(page_id="b"))
    await store.save_page(_make_page(page_id="c"))
    modified = await store.mark_dirty("C1", ["a", "c"])
    assert modified == 2
    assert fake.docs[("C1", "en", "a")]["is_dirty"] is True
    assert fake.docs[("C1", "en", "b")]["is_dirty"] is False
    assert fake.docs[("C1", "en", "c")]["is_dirty"] is True


async def test_clear_dirty_sets_is_dirty_false() -> None:
    store, fake = _make_store()
    await store.save_page(_make_page(page_id="a"))
    await store.mark_dirty("C1", ["a"])
    assert fake.docs[("C1", "en", "a")]["is_dirty"] is True
    cleared = await store.clear_dirty("C1", ["a"])
    assert cleared == 1
    assert fake.docs[("C1", "en", "a")]["is_dirty"] is False


async def test_mark_dirty_with_empty_list_is_noop() -> None:
    store, _ = _make_store()
    modified = await store.mark_dirty("C1", [])
    assert modified == 0


# ─────────────────────────────────────────────────────────────────────────────
# Tensions (used by PR-G's contradiction surfacing)
# ─────────────────────────────────────────────────────────────────────────────


async def test_append_tensions_adds_to_page() -> None:
    store, _ = _make_store()
    await store.save_page(_make_page())
    new_tensions = [
        WikiTension(
            fact_id="f1",
            contradicts_fact_id="f2",
            summary="conflicting decisions",
        )
    ]
    ok = await store.append_tensions("C1", "topic:auth", new_tensions)
    assert ok is True
    fetched = await store.get_page("C1", "topic:auth")
    assert fetched is not None
    assert len(fetched.tensions) == 1
    assert fetched.tensions[0].fact_id == "f1"


async def test_append_tensions_with_empty_list_is_noop() -> None:
    store, _ = _make_store()
    await store.save_page(_make_page())
    ok = await store.append_tensions("C1", "topic:auth", [])
    assert ok is False


# ─────────────────────────────────────────────────────────────────────────────
# delete_page
# ─────────────────────────────────────────────────────────────────────────────


async def test_delete_page_removes_the_row() -> None:
    store, fake = _make_store()
    await store.save_page(_make_page())
    deleted = await store.delete_page("C1", "topic:auth")
    assert deleted is True
    assert len(fake.docs) == 0


async def test_delete_missing_page_returns_false() -> None:
    store, _ = _make_store()
    deleted = await store.delete_page("C1", "missing")
    assert deleted is False


# ─────────────────────────────────────────────────────────────────────────────
# wiki-llm-native-redesign — Phase 1 §2.6 / §2.7
# ─────────────────────────────────────────────────────────────────────────────


async def test_save_page_round_trips_redesign_fields() -> None:
    """§2.6 — every new redesign field survives save_page → get_page."""
    store, _ = _make_store()
    page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="topic:auth",
        title="Authentication Architecture",
        slug="authentication-architecture",
        kind="entity",
        kind_schema={
            "name": "Alice",
            "owns": ["auth-service"],
            "decides": ["session-policy"],
            "contributes": ["RFC-42"],
        },
        cross_links={"Session Policy": "session-policy", "RFC-42": "rfc-42"},
        cross_links_broken=["unknown-page"],
        pin_state={
            "pinned": True,
            "hidden": False,
            "reason": "load-bearing — do not restructure",
            "set_by": "alan@beever.ai",
            "set_at": None,
        },
        merged_into=None,
    )
    await store.save_page(page)

    fetched = await store.get_page("C1", "topic:auth")
    assert fetched is not None
    assert fetched.kind == "entity"
    assert fetched.kind_schema == {
        "name": "Alice",
        "owns": ["auth-service"],
        "decides": ["session-policy"],
        "contributes": ["RFC-42"],
    }
    assert fetched.cross_links == {
        "Session Policy": "session-policy",
        "RFC-42": "rfc-42",
    }
    assert fetched.cross_links_broken == ["unknown-page"]
    assert fetched.pin_state["pinned"] is True
    assert fetched.pin_state["reason"] == "load-bearing — do not restructure"
    assert fetched.pin_state["set_by"] == "alan@beever.ai"
    assert fetched.merged_into is None
    # And the original slug-as-identity field round-trips too.
    assert fetched.slug == "authentication-architecture"


async def test_save_page_defaults_redesign_fields_for_legacy_callers() -> None:
    """A WikiPage instantiated without the redesign kwargs persists with
    defaults that match the legacy maintainer's expectations — kind defaults
    to ``topic``, ``cross_links`` empty, ``pin_state`` neutral, ``merged_into``
    None. Guards against accidental ``None`` defaults that would crash the
    legacy maintainer when WIKI_LLM_NATIVE_REDESIGN is OFF.
    """
    store, _ = _make_store()
    page = WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id="overview",
        title="Overview",
    )
    await store.save_page(page)

    fetched = await store.get_page("C1", "overview")
    assert fetched is not None
    assert fetched.kind == "topic"
    assert fetched.kind_schema is None
    assert fetched.cross_links == {}
    assert fetched.cross_links_broken == []
    assert fetched.pin_state == {
        "pinned": False,
        "hidden": False,
        "reason": "",
        "set_by": "",
        "set_at": None,
    }
    assert fetched.merged_into is None


async def test_ensure_indexes_creates_redesign_indexes() -> None:
    """§2.7 — ensure_indexes requests the three redesign indexes with the
    correct keys, uniqueness, sparseness, and partial filters. Captures the
    create_index calls structurally so a regression that drops the partial
    filter (which would crash the unique constraint on legacy ``slug=""``
    rows) fails the test.
    """
    store, fake = _make_store()
    # Indexes call into the fake collection directly via store._collection.
    await store.ensure_indexes()

    by_name = {call.get("name"): call for call in fake.index_calls}
    # Pre-existing indexes still requested.
    assert "wiki_pages_compound_unique" in by_name
    assert "wiki_pages_channel_updated" in by_name
    # Redesign — slug uniqueness with partial filter on non-empty slug.
    slug_idx = by_name.get("wiki_pages_channel_lang_slug_unique")
    assert slug_idx is not None, "missing slug uniqueness index"
    assert slug_idx["keys"] == [
        ("channel_id", 1),
        ("target_lang", 1),
        ("slug", 1),
    ]
    assert slug_idx.get("unique") is True
    assert slug_idx.get("partialFilterExpression") == {"slug": {"$gt": ""}}
    # Redesign — sparse index on merged_into.
    merged_idx = by_name.get("wiki_pages_merged_into_sparse")
    assert merged_idx is not None, "missing merged_into sparse index"
    assert merged_idx["keys"] == [("merged_into", 1)]
    assert merged_idx.get("sparse") is True
    # Redesign — (channel_id, kind, updated_at DESC) for list-by-kind.
    kind_idx = by_name.get("wiki_pages_channel_kind_updated")
    assert kind_idx is not None, "missing channel/kind/updated_at index"
    assert kind_idx["keys"] == [
        ("channel_id", 1),
        ("kind", 1),
        ("updated_at", -1),
    ]
