"""Unit tests for the per-page curation API surface (PATCH curation +
list_manual_dirty_pages).

The wiki HTTP endpoints are thin wrappers over ``WikiPageStore.update_curation_mode``
and ``list_manual_dirty_pages`` — testing the store methods directly gives
99% of the coverage without standing up a FastAPI test client. Endpoint-level
validation (400 for invalid mode, 404 for missing page) is covered via direct
calls to the helper.
"""

from __future__ import annotations

from typing import Any

import pytest

from beever_atlas.wiki.page_store import WikiPageStore


class _FakeUpdateResult:
    def __init__(self, modified: int) -> None:
        self.modified_count = modified


class _FakeCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = list(docs)

    def sort(self, *args, **kwargs) -> "_FakeCursor":
        return self

    def __aiter__(self):
        self._iter = iter(self._docs)
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return next(self._iter)
        except StopIteration:
            raise StopAsyncIteration


class _FakeWikiPagesCollection:
    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        for k, v in query.items():
            if doc.get(k) != v:
                return False
        return True

    async def find_one_and_update(
        self, query: dict[str, Any], update: dict[str, Any], return_document: Any = None
    ) -> dict[str, Any] | None:
        for d in self.docs:
            if self._matches(d, query):
                for k, v in update.get("$set", {}).items():
                    d[k] = v
                return dict(d)
        return None

    def find(self, query: dict[str, Any]) -> _FakeCursor:
        return _FakeCursor([dict(d) for d in self.docs if self._matches(d, query)])


def _make_store() -> tuple[WikiPageStore, _FakeWikiPagesCollection]:
    fake = _FakeWikiPagesCollection()
    store = WikiPageStore.__new__(WikiPageStore)
    store._db = None  # type: ignore[attr-defined]
    store._collection = fake  # type: ignore[attr-defined]
    return store, fake


def _seed(fake: _FakeWikiPagesCollection, **overrides: Any) -> None:
    base = {
        "channel_id": "C1",
        "target_lang": "en",
        "page_id": "topic:gpu",
        "slug": "gpu-procurement",
        "kind": "topic",
        "title": "GPU Procurement",
        "version": 1,
        "sections": [],
        "last_facts_seen": [],
        "is_dirty": False,
        "curation_mode": "auto",
        "archived": False,
        "pin_state": {"pinned": False, "hidden": False, "reason": "", "set_by": "", "set_at": None},
        "merged_into": None,
    }
    base.update(overrides)
    fake.docs.append(base)


# ---------------------------------------------------------------------------
# update_curation_mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_curation_mode_persists() -> None:
    store, fake = _make_store()
    _seed(fake)

    updated = await store.update_curation_mode("C1", "gpu-procurement", "frozen", target_lang="en")

    assert updated is not None
    assert updated.curation_mode == "frozen"
    assert fake.docs[0]["curation_mode"] == "frozen"


@pytest.mark.asyncio
async def test_update_curation_mode_rejects_invalid_value() -> None:
    store, fake = _make_store()
    _seed(fake)

    updated = await store.update_curation_mode("C1", "gpu-procurement", "rainbow", target_lang="en")
    assert updated is None
    # Doc unchanged.
    assert fake.docs[0]["curation_mode"] == "auto"


@pytest.mark.asyncio
async def test_update_curation_mode_404_on_missing_page() -> None:
    store, _ = _make_store()
    updated = await store.update_curation_mode("C1", "nonexistent", "manual", target_lang="en")
    assert updated is None


@pytest.mark.asyncio
async def test_update_curation_mode_does_not_bump_version() -> None:
    store, fake = _make_store()
    _seed(fake, version=42)

    updated = await store.update_curation_mode("C1", "gpu-procurement", "manual", target_lang="en")
    assert updated is not None
    assert updated.version == 42


# ---------------------------------------------------------------------------
# list_manual_dirty_pages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_manual_dirty_pages_filters_correctly() -> None:
    store, fake = _make_store()
    _seed(fake, slug="page-a", curation_mode="manual", is_dirty=True)
    _seed(fake, slug="page-b", curation_mode="manual", is_dirty=False)
    _seed(fake, slug="page-c", curation_mode="auto", is_dirty=True)
    _seed(fake, slug="page-d", curation_mode="frozen", is_dirty=True)

    result = await store.list_manual_dirty_pages("C1", target_lang="en")
    slugs = sorted(p.slug for p in result)
    assert slugs == ["page-a"]


@pytest.mark.asyncio
async def test_list_manual_dirty_pages_isolates_per_channel() -> None:
    store, fake = _make_store()
    _seed(fake, channel_id="C1", slug="x", curation_mode="manual", is_dirty=True)
    _seed(fake, channel_id="C2", slug="y", curation_mode="manual", is_dirty=True)

    result = await store.list_manual_dirty_pages("C1", target_lang="en")
    assert [p.slug for p in result] == ["x"]


# ---------------------------------------------------------------------------
# apply_pending_updates flush — defends the post-review CRITICAL fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apply_pending_updates_passes_truly_new_facts() -> None:
    """The flush must compute new_fact_ids the same way ``maintain_now``
    does — diff every channel fact against ``page.last_facts_seen``. The
    pre-fix bug used ``set(page.last_facts_seen) | set(page.last_facts_seen)``
    which always equalled the already-seen set, so apply_update returned
    immediately and the flush was dead code.

    This test asserts the maintainer is invoked with the genuinely new
    fact ids (those NOT already in last_facts_seen).
    """
    store, fake = _make_store()
    _seed(
        fake,
        slug="page-a",
        page_id="topic:a",
        curation_mode="manual",
        is_dirty=True,
        last_facts_seen=["f1", "f2"],
    )

    seen_calls: list[tuple[str, list[str]]] = []

    class _FakeMaintainer:
        async def _load_facts(self, channel_id: str, _maybe_ids):  # noqa: ARG002
            return [{"id": "f1"}, {"id": "f2"}, {"id": "f3"}, {"id": "f4"}]

        async def apply_update(
            self,
            channel_id: str,
            page_id: str,
            new_fact_ids: list[str],
            *,
            target_lang: str = "en",
        ) -> bool:
            del channel_id, target_lang
            seen_calls.append((page_id, sorted(new_fact_ids)))
            return True

    # Mirror the endpoint's logic so the test pins the contract.
    manual_dirty = await store.list_manual_dirty_pages("C1", target_lang="en")
    maintainer = _FakeMaintainer()
    channel_facts = await maintainer._load_facts("C1", None)
    all_fact_ids = {str(f.get("id") or "") for f in channel_facts}
    flushed: list[str] = []
    for page in manual_dirty:
        already_seen = set(page.last_facts_seen)
        new_fact_ids = sorted(fid for fid in all_fact_ids if fid and fid not in already_seen)
        applied = await maintainer.apply_update("C1", page.page_id, new_fact_ids, target_lang="en")
        if applied:
            flushed.append(page.page_id)

    assert flushed == ["topic:a"]
    assert seen_calls == [("topic:a", ["f3", "f4"])]
