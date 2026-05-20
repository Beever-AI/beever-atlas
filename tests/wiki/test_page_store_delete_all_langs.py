"""Tests for ``WikiPageStore.delete_all_for_channel_all_langs`` (Wave 1).

The channel hard-purge wipes wiki pages across EVERY language (the existing
``delete_all_for_channel`` scopes to one ``target_lang`` and is kept for the
reset/rebuild path). This method drops the ``target_lang`` filter on both
``wiki_pages`` and ``wiki_redirects``.

Covers: en + es pages both wiped; redirects across langs cleared; other
channels untouched; the single-lang method still scopes to one language.

In-memory fake collection (no live Mongo), mirroring
``tests/wiki/test_redirects_and_folder.py``.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.wiki.page_store import WikiPageStore


class _DeleteResult:
    def __init__(self, n: int) -> None:
        self.deleted_count = n


class _FakeCollection:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs: list[dict[str, Any]] = [dict(d) for d in (docs or [])]

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        return all(doc.get(k) == v for k, v in query.items())

    async def delete_many(self, query: dict[str, Any]) -> _DeleteResult:
        keep, removed = [], 0
        for doc in self.docs:
            if self._matches(doc, query):
                removed += 1
            else:
                keep.append(doc)
        self.docs = keep
        return _DeleteResult(removed)


def _make_store(
    pages: list[dict[str, Any]], redirects: list[dict[str, Any]]
) -> tuple[WikiPageStore, _FakeCollection, _FakeCollection]:
    store = WikiPageStore()
    pc = _FakeCollection(pages)
    rc = _FakeCollection(redirects)
    store._collection = pc  # type: ignore[attr-defined]
    store._redirects = rc  # type: ignore[attr-defined]
    return store, pc, rc


def _pages_seed() -> list[dict[str, Any]]:
    return [
        {"channel_id": "C1", "target_lang": "en", "page_id": "topic:a"},
        {"channel_id": "C1", "target_lang": "es", "page_id": "topic:a"},
        {"channel_id": "C1", "target_lang": "en", "page_id": "topic:b"},
        {"channel_id": "C2", "target_lang": "en", "page_id": "topic:a"},  # other chan
    ]


def _redirects_seed() -> list[dict[str, Any]]:
    return [
        {"channel_id": "C1", "target_lang": "en", "old_path": "/wiki/x"},
        {"channel_id": "C1", "target_lang": "es", "old_path": "/wiki/y"},
        {"channel_id": "C2", "target_lang": "en", "old_path": "/wiki/z"},
    ]


async def test_wipes_all_languages_and_redirects() -> None:
    store, pages, redirects = _make_store(_pages_seed(), _redirects_seed())

    deleted = await store.delete_all_for_channel_all_langs("C1")

    # 3 C1 pages across en+es deleted; C2 page survives.
    assert deleted == 3
    assert [d["channel_id"] for d in pages.docs] == ["C2"]
    # Both C1 redirect rows (en + es) cleared; C2 redirect survives.
    assert [r["channel_id"] for r in redirects.docs] == ["C2"]


async def test_other_channels_untouched() -> None:
    store, pages, redirects = _make_store(_pages_seed(), _redirects_seed())
    await store.delete_all_for_channel_all_langs("C1")
    c2_pages = [d for d in pages.docs if d["channel_id"] == "C2"]
    c2_redirects = [r for r in redirects.docs if r["channel_id"] == "C2"]
    assert len(c2_pages) == 1
    assert len(c2_redirects) == 1


async def test_single_lang_method_still_scopes_to_one_language() -> None:
    """Regression guard: the kept single-lang method only deletes en."""
    store, pages, _redirects = _make_store(_pages_seed(), _redirects_seed())

    deleted = await store.delete_all_for_channel("C1", target_lang="en")

    # Only the 2 C1/en pages; the C1/es page survives.
    assert deleted == 2
    survivors = {(d["channel_id"], d["target_lang"]) for d in pages.docs}
    assert ("C1", "es") in survivors
    assert ("C2", "en") in survivors
