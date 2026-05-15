"""Phase-A tests for the wiki_redirects collection in ``WikiPageStore``.

Covers requirements from
``openspec/changes/llm-wiki-folder-structure/specs/wiki-page-store/spec.md``
delta:

  - Save_page writes redirect on parent change
  - resolve_redirect returns latest target (chain following)
  - Self-redirect is never written
  - Cycle protection in resolve_redirect

The folder fingerprint-skip body-preservation optimization is deferred
to Phase C tests where it has a real synthesis caller — Phase A only
plumbs the collection + helper.

Tests use an in-memory fake collection (no MongoDB) — the WikiPageStore
talks to motor via .find_one / .update_one / .delete_one which we stub
with a tiny dict-backed implementation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from beever_atlas.models.persistence import WikiPage
from beever_atlas.wiki.page_store import WikiPageStore, _canonical_page_path


class _FakeCollection:
    """In-memory stand-in for a Motor collection.

    Only implements the subset WikiPageStore.save_page + resolve_redirect
    actually call: ``find_one``, ``update_one`` (with ``upsert=True``).
    Filters use the (channel_id, target_lang, page_id|old_path) compound
    key shape; the tests don't need full Mongo query semantics.
    """

    def __init__(self) -> None:
        self.docs: list[dict[str, Any]] = []

    async def find_one(
        self, filt: dict[str, Any], projection: dict | None = None
    ) -> dict[str, Any] | None:
        for doc in self.docs:
            if all(doc.get(k) == v for k, v in filt.items()):
                return dict(doc)  # Return a copy so callers don't mutate the store.
        return None

    async def update_one(
        self,
        filt: dict[str, Any],
        update: dict[str, Any],
        upsert: bool = False,
    ) -> Any:
        existing = None
        existing_idx = -1
        for i, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in filt.items()):
                existing = doc
                existing_idx = i
                break

        new_doc: dict[str, Any] = dict(existing) if existing else dict(filt)

        # Apply $setOnInsert ONLY when inserting.
        if existing is None and "$setOnInsert" in update:
            for k, v in update["$setOnInsert"].items():
                new_doc.setdefault(k, v)

        # Apply $set always.
        for k, v in update.get("$set", {}).items():
            new_doc[k] = v

        # Apply $inc.
        for k, v in update.get("$inc", {}).items():
            new_doc[k] = (new_doc.get(k) or 0) + v

        if existing is None and not upsert:
            return None
        if existing_idx >= 0:
            self.docs[existing_idx] = new_doc
        else:
            self.docs.append(new_doc)
        return None

    async def delete_one(self, filt: dict[str, Any]) -> Any:
        for i, doc in enumerate(self.docs):
            if all(doc.get(k) == v for k, v in filt.items()):
                self.docs.pop(i)
                return None
        return None


def _make_store() -> tuple[WikiPageStore, _FakeCollection, _FakeCollection]:
    """Construct a WikiPageStore with both collections stubbed."""
    pages = _FakeCollection()
    redirects = _FakeCollection()
    store = WikiPageStore()
    store._collection = pages  # type: ignore[attr-defined]
    store._redirects = redirects  # type: ignore[attr-defined]
    return store, pages, redirects


def _topic(slug: str = "topic-auth", parent_id: str | None = None) -> WikiPage:
    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=slug,
        title=slug.replace("-", " ").title(),
        slug=slug,
        page_type="topic",
        parent_id=parent_id,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


def _folder(
    slug: str,
    *,
    children: list[dict[str, str]] | None = None,
    fingerprint: str | None = None,
    content: str = "",
) -> WikiPage:
    return WikiPage(
        channel_id="C1",
        target_lang="en",
        page_id=slug,
        title=slug.replace("-", " ").title(),
        slug=slug,
        page_type="folder",
        parent_id=None,
        children=children or [],
        children_fingerprint=fingerprint,
        is_synthetic=True,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )


# ---- canonical-path helper -------------------------------------------------


def test_canonical_path_root() -> None:
    assert _canonical_page_path({"slug": "topic-auth"}) == "/wiki/topic-auth"


def test_canonical_path_with_parent() -> None:
    assert (
        _canonical_page_path({"slug": "topic-auth", "parent_id": "folder-security"})
        == "/wiki/folder-security/topic-auth"
    )


def test_canonical_path_falls_back_to_page_id_when_slug_missing() -> None:
    assert _canonical_page_path({"page_id": "x", "slug": ""}) == "/wiki/x"


def test_canonical_path_returns_empty_when_no_identity() -> None:
    assert _canonical_page_path({}) == ""


# ---- save_page redirect on path change -------------------------------------


@pytest.mark.asyncio
async def test_save_page_writes_redirect_on_parent_change() -> None:
    """Move a leaf from root → folder; a redirect row appears."""
    store, _pages, redirects = _make_store()
    # First save: leaf at root.
    await store.save_page(_topic(slug="topic-auth"))
    assert len(redirects.docs) == 0  # No prior row, so no path change.

    # Second save: same slug, now under a folder.
    await store.save_page(_topic(slug="topic-auth", parent_id="folder-security"))
    assert len(redirects.docs) == 1
    row = redirects.docs[0]
    assert row["channel_id"] == "C1"
    assert row["target_lang"] == "en"
    assert row["old_path"] == "/wiki/topic-auth"
    assert row["new_path"] == "/wiki/folder-security/topic-auth"


@pytest.mark.asyncio
async def test_save_page_does_not_write_self_redirect() -> None:
    """Re-save with no path change: no redirect row created."""
    store, _pages, redirects = _make_store()
    await store.save_page(_topic(slug="topic-auth"))
    await store.save_page(_topic(slug="topic-auth"))  # Same path again.
    assert len(redirects.docs) == 0


# ---- resolve_redirect -----------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_redirect_chains_to_latest_target() -> None:
    """Two chained moves resolve to the final destination."""
    store, _pages, _redirects = _make_store()
    # Build a chain: /a → /b → /c
    await store.save_page(_topic(slug="a"))
    await store.save_page(_topic(slug="a", parent_id="folder1"))  # /a → /folder1/a
    # Wipe and re-create to simulate another move (slug stays, parent
    # changes again).
    await store.save_page(_topic(slug="a", parent_id="folder2"))  # /folder1/a → /folder2/a

    resolved = await store.resolve_redirect("C1", "en", "/wiki/a")
    assert resolved == "/wiki/folder2/a"


@pytest.mark.asyncio
async def test_resolve_redirect_returns_none_for_unknown_path() -> None:
    store, _pages, _redirects = _make_store()
    assert await store.resolve_redirect("C1", "en", "/wiki/never-existed") is None


@pytest.mark.asyncio
async def test_resolve_redirect_handles_cycle_safely() -> None:
    """Manually craft a redirect cycle; resolver must not loop."""
    store, _pages, redirects = _make_store()
    # Inject a deliberate cycle (real save_page would never produce this
    # because the unique index plus self-redirect filter prevent it,
    # but defensive coding still guards against database corruption).
    redirects.docs.append(
        {
            "channel_id": "C1",
            "target_lang": "en",
            "old_path": "/wiki/x",
            "new_path": "/wiki/y",
        }
    )
    redirects.docs.append(
        {
            "channel_id": "C1",
            "target_lang": "en",
            "old_path": "/wiki/y",
            "new_path": "/wiki/x",
        }
    )
    # Should terminate (return either x or y), not infinite-loop.
    result = await store.resolve_redirect("C1", "en", "/wiki/x")
    assert result in {"/wiki/x", "/wiki/y"}


# ---- folder page persistence (children + fingerprint round-trip) -----------


@pytest.mark.asyncio
async def test_folder_page_round_trips_through_persistence() -> None:
    """A folder page with children + fingerprint persists every new field."""
    store, pages, _redirects = _make_store()
    await store.save_page(
        _folder(
            "folder-security",
            children=[
                {"slug": "auth", "title": "Auth", "page_type": "topic", "section_number": "1.1"},
                {"slug": "rbac", "title": "RBAC", "page_type": "topic", "section_number": "1.2"},
            ],
            fingerprint="sha256-of-auth-rbac",
        )
    )
    assert len(pages.docs) == 1
    row = pages.docs[0]
    assert row["page_type"] == "folder"
    assert row["children_fingerprint"] == "sha256-of-auth-rbac"
    assert row["is_synthetic"] is True
    assert len(row["children"]) == 2
    assert row["children"][0]["slug"] == "auth"
