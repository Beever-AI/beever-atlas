"""Unit test for ``WikiCache.delete_all_for_channel`` (delete-channel hard-purge).

Guards the anchored-prefix regex used to wipe a channel's rendered wiki blob +
generation status across ALL languages: it must remove ``{id}`` and
``{id}:{lang}`` rows but NEVER a channel that merely shares a prefix (``C1`` vs
``C12``), and it must treat regex metacharacters in the id literally. No live
Mongo — a fake collection applies the ``$regex`` with Python's ``re`` so the
exact pattern is exercised.

Convention: no ``@pytest.mark.asyncio`` decorators; pyproject sets
``asyncio_mode = "auto"``.
"""

from __future__ import annotations

import re
from typing import Any

from beever_atlas.wiki.cache import WikiCache


class _DeleteResult:
    def __init__(self, n: int) -> None:
        self.deleted_count = n


class _FakeCollection:
    """Applies a ``{"channel_id": {"$regex": ...}}`` delete_many like Mongo."""

    def __init__(self, channel_ids: list[str]) -> None:
        self.docs: list[dict[str, Any]] = [{"channel_id": c} for c in channel_ids]

    async def delete_many(self, query: dict[str, Any]) -> _DeleteResult:
        rx = re.compile(query["channel_id"]["$regex"])
        keep = [d for d in self.docs if not rx.search(d["channel_id"])]
        removed = len(self.docs) - len(keep)
        self.docs = keep
        return _DeleteResult(removed)


def _cache_with_fakes(cache_ids: list[str], status_ids: list[str]) -> WikiCache:
    cache = WikiCache.__new__(WikiCache)
    # _ensure_db() short-circuits when _collection is already set.
    cache._collection = _FakeCollection(cache_ids)  # type: ignore[attr-defined]
    cache._status_collection = _FakeCollection(status_ids)  # type: ignore[attr-defined]
    return cache


async def test_delete_all_for_channel_removes_all_langs_and_legacy_key() -> None:
    cache = _cache_with_fakes(
        cache_ids=["C1", "C1:en", "C1:fr", "C12", "C12:en", "C2:en"],
        status_ids=["C1:en", "C1:fr", "C12:en"],
    )
    deleted = await cache.delete_all_for_channel("C1")

    # 3 cache rows (C1, C1:en, C1:fr) + 2 status rows (C1:en, C1:fr) = 5
    assert deleted == 5
    # Prefix-collision safety: C12 / C12:en / C2:en survive untouched.
    assert {d["channel_id"] for d in cache._collection.docs} == {"C12", "C12:en", "C2:en"}
    assert {d["channel_id"] for d in cache._status_collection.docs} == {"C12:en"}


async def test_delete_all_for_channel_escapes_regex_metacharacters() -> None:
    # A channel id with regex metacharacters must be matched literally — a naive
    # (un-escaped) pattern would let "a.b" also match "axb".
    cache = _cache_with_fakes(
        cache_ids=["a.b", "a.b:en", "axb", "axb:en"],
        status_ids=[],
    )
    deleted = await cache.delete_all_for_channel("a.b")
    assert deleted == 2  # only "a.b" and "a.b:en"
    assert {d["channel_id"] for d in cache._collection.docs} == {"axb", "axb:en"}
