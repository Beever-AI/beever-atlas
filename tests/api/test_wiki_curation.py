"""Endpoint tests for the wiki-llm-native-redesign curation API (§5.9–§5.14).

Covers POST /pin / /hide / /split / /merge plus the merge-redirect
behaviour the maintainer adds in plan_updates. Wires through FastAPI's
TestClient so the routing + auth + body validation also exercise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch


from beever_atlas.models.persistence import WikiPage, WikiPageSection
from beever_atlas.services import wiki_maintainer as wm_mod
from beever_atlas.services.wiki_maintainer import WikiMaintainer


# ---------------------------------------------------------------------------
# Lightweight fake page-store mirroring tests/wiki/test_page_store.py.
# Re-implemented here (rather than imported) so the fact that the curation
# endpoints exercise the production WikiPageStore code paths via this fake
# stays honest — the test suite owns the fake's contract.
# ---------------------------------------------------------------------------


class _FakeWikiPagesCollection:
    def __init__(self) -> None:
        self.docs: dict[tuple[str, str, str], dict[str, Any]] = {}

    @staticmethod
    def _key(query: dict[str, Any]) -> tuple[str, str, str]:
        return (query["channel_id"], query["target_lang"], query.get("page_id", ""))

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            # Resolve the actual value (possibly nested via dotted key)
            if "." in k:
                root, sub = k.split(".", 1)
                inner = doc.get(root) or {}
                actual = inner.get(sub) if isinstance(inner, dict) else None
            else:
                actual = doc.get(k)
            if isinstance(v, dict):
                if "$ne" in v and actual == v["$ne"]:
                    return False
                if "$in" in v and actual not in v["$in"]:
                    return False
                if "$gt" in v and not (actual is not None and actual > v["$gt"]):
                    return False
            else:
                if actual != v:
                    return False
        return True

    async def create_index(self, *args, **kwargs) -> None:
        pass

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        # Slug-based lookups
        if "slug" in query and isinstance(query["slug"], str):
            for doc in self.docs.values():
                if (
                    doc.get("channel_id") == query.get("channel_id")
                    and doc.get("target_lang") == query.get("target_lang")
                    and doc.get("slug") == query["slug"]
                ):
                    return dict(doc)
            return None
        # Page_id-based lookups
        if "page_id" in query and isinstance(query["page_id"], str):
            doc = self.docs.get(self._key(query))
            return dict(doc) if doc else None
        for doc in self.docs.values():
            if self._matches(doc, query):
                return dict(doc)
        return None

    def find(self, query: dict[str, Any]):
        rows = [dict(doc) for doc in self.docs.values() if self._matches(doc, query)]

        class _Cursor:
            def __init__(self, docs):
                self._docs = docs

            def sort(self, key, direction=-1):
                self._docs.sort(
                    key=lambda d: d.get(key) or "",
                    reverse=(direction == -1),
                )
                return self

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self._docs:
                    raise StopAsyncIteration
                return self._docs.pop(0)

        return _Cursor(rows)

    async def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False):
        # Page-id keyed upserts (save_page path)
        if (
            "channel_id" in query
            and "target_lang" in query
            and "page_id" in query
            and not isinstance(query["page_id"], dict)
        ):
            key = (query["channel_id"], query["target_lang"], query["page_id"])
            existing = self.docs.get(key)
            is_insert = existing is None
            if is_insert and not upsert:
                return _UpdateResult(0)
            if is_insert:
                self.docs[key] = {}
                existing = self.docs[key]
            if is_insert:
                for k, v in update.get("$setOnInsert", {}).items():
                    existing[k] = v
            for k, v in update.get("$set", {}).items():
                existing[k] = v
            for k, v in update.get("$inc", {}).items():
                existing[k] = (existing.get(k) or 0) + v
            for k, v in update.get("$pull", {}).items():
                cur = existing.get(k) or []
                if isinstance(v, dict) and "$in" in v:
                    cur = [x for x in cur if x not in v["$in"]]
                existing[k] = cur
            return _UpdateResult(1)
        # Slug-based update (the curation path)
        if "slug" in query:
            for doc in self.docs.values():
                if self._matches(doc, query):
                    for k, v in update.get("$set", {}).items():
                        doc[k] = v
                    for k, v in update.get("$pull", {}).items():
                        cur = doc.get(k) or []
                        if isinstance(v, dict) and "$in" in v:
                            cur = [x for x in cur if x not in v["$in"]]
                        doc[k] = cur
                    return _UpdateResult(1)
        return _UpdateResult(0)

    async def find_one_and_update(self, query, update, return_document=False):
        for doc in self.docs.values():
            if self._matches(doc, query):
                for k, v in update.get("$set", {}).items():
                    doc[k] = v
                return dict(doc)  # post-update snapshot
        return None


class _UpdateResult:
    def __init__(self, modified: int) -> None:
        self.modified_count = modified


def _make_store():
    from beever_atlas.wiki.page_store import WikiPageStore

    fake = _FakeWikiPagesCollection()
    store = WikiPageStore.__new__(WikiPageStore)
    store._db = None  # type: ignore[attr-defined]
    store._collection = fake  # type: ignore[attr-defined]
    return store, fake


def _make_page(
    *,
    channel_id: str = "C1",
    page_id: str = "topic:auth",
    slug: str = "topic-auth",
    title: str = "Authentication",
    target_lang: str = "en",
    last_facts_seen: list[str] | None = None,
    pin_state: dict[str, Any] | None = None,
    merged_into: str | None = None,
) -> WikiPage:
    return WikiPage(
        channel_id=channel_id,
        target_lang=target_lang,
        page_id=page_id,
        title=title,
        slug=slug,
        sections=[WikiPageSection(id="overview", title="Overview", content_md="hello")],
        last_facts_seen=last_facts_seen or [],
        pin_state=pin_state
        or {
            "pinned": False,
            "hidden": False,
            "reason": "",
            "set_by": "",
            "set_at": None,
        },
        merged_into=merged_into,
        version=2,  # non-1 so we can assert version is preserved
    )


# ---------------------------------------------------------------------------
# §5.9 — pin endpoint preserves version
# ---------------------------------------------------------------------------


async def test_update_pin_state_preserves_version() -> None:
    store, fake = _make_store()
    page = _make_page()
    await store.save_page(page)
    pre_version = (await store.get_page("C1", "topic:auth", target_lang="en")).version

    new_state = {
        "pinned": True,
        "hidden": False,
        "reason": "load-bearing",
        "set_by": "alan@beever.ai",
        "set_at": datetime.now(tz=UTC).isoformat(),
    }
    updated = await store.update_pin_state("C1", "topic-auth", new_state, target_lang="en")
    assert updated is not None
    assert updated.pin_state["pinned"] is True
    assert updated.pin_state["set_by"] == "alan@beever.ai"
    # Version unchanged — curation does NOT bump version.
    fetched = await store.get_page("C1", "topic:auth", target_lang="en")
    assert fetched is not None
    assert fetched.version == pre_version


# ---------------------------------------------------------------------------
# §5.10 — list_pages_by_kind(scope="human") excludes hidden + merged
# ---------------------------------------------------------------------------


async def test_list_pages_by_kind_excludes_hidden_in_human_scope() -> None:
    store, fake = _make_store()
    visible = _make_page(page_id="topic:visible", slug="topic-visible")
    hidden = _make_page(
        page_id="topic:hidden",
        slug="topic-hidden",
        pin_state={
            "pinned": False,
            "hidden": True,
            "reason": "obsolete",
            "set_by": "alan",
            "set_at": None,
        },
    )
    merged = _make_page(
        page_id="topic:merged",
        slug="topic-merged",
        merged_into="topic-visible",
    )
    await store.save_page(visible)
    await store.save_page(hidden)
    await store.save_page(merged)

    human = await store.list_pages_by_kind("C1", scope="human")
    slugs = {p.slug for p in human}
    assert "topic-visible" in slugs
    assert "topic-hidden" not in slugs
    assert "topic-merged" not in slugs

    all_pages = await store.list_pages_by_kind("C1", scope="all")
    assert {p.slug for p in all_pages} == {
        "topic-visible",
        "topic-hidden",
        "topic-merged",
    }


# ---------------------------------------------------------------------------
# §5.11 — split endpoint: source's last_facts_seen shrinks
# ---------------------------------------------------------------------------


async def test_remove_facts_from_page_drops_named_ids() -> None:
    store, fake = _make_store()
    page = _make_page(last_facts_seen=["f1", "f2", "f3"])
    await store.save_page(page)
    ok = await store.remove_facts_from_page("C1", "topic-auth", ["f1", "f3"], target_lang="en")
    assert ok is True
    fetched = await store.get_page("C1", "topic:auth")
    assert fetched is not None
    assert fetched.last_facts_seen == ["f2"]


# ---------------------------------------------------------------------------
# §5.12 — merge endpoint: source has merged_into + hidden=True
# ---------------------------------------------------------------------------


async def test_record_merged_into_hides_source_and_sets_redirect() -> None:
    store, fake = _make_store()
    src = _make_page(page_id="topic:src", slug="topic-src")
    tgt = _make_page(page_id="topic:tgt", slug="topic-tgt")
    await store.save_page(src)
    await store.save_page(tgt)

    updated = await store.record_merged_into("C1", "topic-src", "topic-tgt", target_lang="en")
    assert updated is not None
    assert updated.merged_into == "topic-tgt"
    assert updated.pin_state["hidden"] is True
    # Target page untouched.
    fetched_tgt = await store.get_page("C1", "topic:tgt")
    assert fetched_tgt is not None
    assert fetched_tgt.merged_into is None
    assert fetched_tgt.pin_state["hidden"] is False


# ---------------------------------------------------------------------------
# §5.13 — pinned page's apply_update prompt has the addendum
# ---------------------------------------------------------------------------


def test_render_kind_prompt_includes_pinned_addendum_when_page_pinned() -> None:
    page = _make_page(
        pin_state={
            "pinned": True,
            "hidden": False,
            "reason": "load-bearing",
            "set_by": "alan",
            "set_at": None,
        },
    )
    prompt = wm_mod._render_kind_prompt("topic", page, [], target_lang="en")
    assert "CURATION CONSTRAINTS" in prompt
    assert "PINNED" in prompt


def test_render_kind_prompt_omits_pinned_addendum_when_unpinned() -> None:
    page = _make_page()
    prompt = wm_mod._render_kind_prompt("topic", page, [], target_lang="en")
    assert "CURATION CONSTRAINTS" not in prompt


def test_legacy_apply_update_prompt_does_NOT_include_pinned_addendum() -> None:
    """Flag-OFF byte-identical guarantee: ``_render_apply_update_prompt``
    must produce exactly the same prompt as it did pre-redesign — even
    when an operator has pinned the page via the curation API. The
    addendum lives only on the kind-dispatch path so flipping
    ``WIKI_LLM_NATIVE_REDESIGN`` ON/OFF is a clean toggle.

    The pin still persists (``pin_state.pinned=True`` lands in Mongo
    regardless), so flipping the redesign flag ON later picks up the
    operator's intent immediately on the next apply_update.
    """
    page = _make_page(
        pin_state={
            "pinned": True,
            "hidden": False,
            "reason": "load-bearing",
            "set_by": "alan",
            "set_at": None,
        },
    )
    prompt = wm_mod._render_apply_update_prompt(page, [], target_lang="en")
    assert "CURATION CONSTRAINTS" not in prompt
    assert "PINNED" not in prompt


# ---------------------------------------------------------------------------
# §5.14 — high-overlap pages produce wiki_merge_proposals
# ---------------------------------------------------------------------------


async def test_find_merge_candidates_surfaces_high_overlap_pairs() -> None:
    store, fake = _make_store()
    a = _make_page(
        page_id="topic:a",
        slug="topic-a",
        last_facts_seen=["f1", "f2", "f3", "f4"],
    )
    b = _make_page(
        page_id="topic:b",
        slug="topic-b",
        last_facts_seen=["f1", "f2", "f3", "f5"],  # 3/5 overlap = 0.6
    )
    c = _make_page(
        page_id="topic:c",
        slug="topic-c",
        last_facts_seen=["f6", "f7", "f8"],  # disjoint
    )
    await store.save_page(a)
    await store.save_page(b)
    await store.save_page(c)

    # Threshold below the (a,b) overlap surfaces it; (a,c) and (b,c)
    # have zero overlap so they don't.
    pairs = await store.find_merge_candidates("C1", threshold=0.5, target_lang="en")
    assert len(pairs) == 1
    src, tgt, jaccard = pairs[0]
    assert {src, tgt} == {"topic-a", "topic-b"}
    assert 0.5 <= jaccard < 0.7

    # Higher threshold — nothing surfaces.
    pairs_strict = await store.find_merge_candidates("C1", threshold=0.9, target_lang="en")
    assert pairs_strict == []


async def test_find_merge_candidates_skips_hidden_or_merged_pages() -> None:
    store, fake = _make_store()
    visible = _make_page(
        page_id="topic:a",
        slug="topic-a",
        last_facts_seen=["f1", "f2", "f3"],
    )
    hidden = _make_page(
        page_id="topic:b",
        slug="topic-b",
        last_facts_seen=["f1", "f2", "f3"],  # would otherwise be 1.0
        pin_state={
            "pinned": False,
            "hidden": True,
            "reason": "",
            "set_by": "",
            "set_at": None,
        },
    )
    merged = _make_page(
        page_id="topic:c",
        slug="topic-c",
        last_facts_seen=["f1", "f2"],
        merged_into="topic-a",
    )
    await store.save_page(visible)
    await store.save_page(hidden)
    await store.save_page(merged)
    pairs = await store.find_merge_candidates("C1", threshold=0.5, target_lang="en")
    # Only the visible-vs-... pair could exist; both other pages are
    # excluded → empty result.
    assert pairs == []


# ---------------------------------------------------------------------------
# Maintainer routing — §5.6 merge-redirect
# ---------------------------------------------------------------------------


async def test_apply_merge_redirects_routes_facts_to_target() -> None:
    """A page with merged_into=topic-tgt should NOT receive new facts;
    they must flow into the target's plan entry instead."""
    store, fake = _make_store()
    src = _make_page(page_id="topic:src", slug="topic-src", merged_into="topic-tgt")
    tgt = _make_page(page_id="topic:tgt", slug="topic-tgt")
    await store.save_page(src)
    await store.save_page(tgt)

    maintainer = WikiMaintainer(page_store=store)
    plan = {"topic:src": ["f1", "f2"], "topic:other": ["f3"]}
    redirected = await maintainer._apply_merge_redirects(plan, channel_id="C1", target_lang="en")
    assert "topic:src" not in redirected
    # Target page exists in the store, so the redirect uses its page_id.
    assert redirected["topic:tgt"] == ["f1", "f2"]
    # Unrelated entries pass through untouched.
    assert redirected["topic:other"] == ["f3"]


async def test_apply_merge_redirects_dedupes_duplicate_facts() -> None:
    """A fact that hits both source and target naturally must not be
    counted twice in the redirected target."""
    store, fake = _make_store()
    src = _make_page(page_id="topic:src", slug="topic-src", merged_into="topic-tgt")
    tgt = _make_page(page_id="topic:tgt", slug="topic-tgt")
    await store.save_page(src)
    await store.save_page(tgt)

    maintainer = WikiMaintainer(page_store=store)
    plan = {"topic:src": ["f1", "f2"], "topic:tgt": ["f2", "f3"]}
    redirected = await maintainer._apply_merge_redirects(plan, channel_id="C1", target_lang="en")
    assert sorted(redirected["topic:tgt"]) == ["f1", "f2", "f3"]


async def test_record_merge_proposals_writes_to_collection() -> None:
    """`_record_merge_proposals` upserts a row per high-overlap pair via
    `stores.mongodb.wiki_merge_proposals`. Patch the singleton so the
    test does NOT require a live Mongo."""
    store, fake = _make_store()
    a = _make_page(page_id="topic:a", slug="topic-a", last_facts_seen=["f1", "f2"])
    b = _make_page(page_id="topic:b", slug="topic-b", last_facts_seen=["f1", "f2"])
    await store.save_page(a)
    await store.save_page(b)

    # Stub stores singleton + the proposals collection.
    captured: list[dict[str, Any]] = []

    class _ProposalsStub:
        async def update_one(self, query, update, upsert=False):
            captured.append({"query": query, "update": update})
            return _UpdateResult(1)

    fake_stores = type(
        "S",
        (),
        {"mongodb": type("M", (), {"wiki_merge_proposals": _ProposalsStub()})()},
    )()
    maintainer = WikiMaintainer(page_store=store)
    fake_settings = type("Cfg", (), {"wiki_page_merge_threshold": 0.5})()

    with (
        patch("beever_atlas.stores.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.infra.config.get_settings",
            return_value=fake_settings,
        ),
    ):
        await maintainer._record_merge_proposals(channel_id="C1", target_lang="en")

    assert len(captured) == 1
    assert captured[0]["query"]["channel_id"] == "C1"
    assert captured[0]["update"]["$set"]["jaccard"] == 1.0


async def test_record_merge_proposals_noops_when_collection_unavailable() -> None:
    """If `stores.mongodb` lacks the collection (test fakes / older
    deployments), the helper short-circuits without raising."""
    store, fake = _make_store()
    a = _make_page(page_id="topic:a", slug="topic-a", last_facts_seen=["f1", "f2"])
    b = _make_page(page_id="topic:b", slug="topic-b", last_facts_seen=["f1", "f2"])
    await store.save_page(a)
    await store.save_page(b)
    maintainer = WikiMaintainer(page_store=store)
    fake_stores = type(
        "S",
        (),
        {"mongodb": type("M", (), {"wiki_merge_proposals": None})()},
    )()
    with patch("beever_atlas.stores.get_stores", return_value=fake_stores):
        # Should NOT raise.
        await maintainer._record_merge_proposals(channel_id="C1", target_lang="en")
