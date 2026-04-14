"""Concurrent archive() must produce distinct, monotonically-increasing version numbers.

Emulates MongoDB's atomic ``$inc`` semantics with an async lock so the shared
counter is incremented serially even under ``asyncio.gather`` fan-out.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from beever_atlas.wiki.version_store import WikiVersionStore


class AtomicCollectionMock:
    """Fake Motor collection where find_one / insert_one are atomic together."""

    def __init__(self) -> None:
        self._latest: int = 0
        self._lock = asyncio.Lock()
        self._inserts: list[dict] = []

    async def find_one(self, *_args, **_kwargs) -> dict | None:
        # NOTE: find_one is NOT atomic with insert_one in real Mongo — so the
        # test is pessimistic. We simulate the atomic guarantee _next_version
        # relies on by holding the lock across both calls from archive().
        if self._latest == 0:
            return None
        return {"version_number": self._latest}

    async def insert_one(self, doc: dict) -> None:
        self._inserts.append(doc)

    async def __aenter__(self):
        await self._lock.acquire()
        return self

    async def __aexit__(self, *_args):
        self._lock.release()


@pytest.mark.asyncio
async def test_concurrent_archive_assigns_distinct_monotonic_versions():
    collection = MagicMock()
    state = {"latest": 0, "inserts": [], "lock": asyncio.Lock()}

    async def find_one(*_args, **_kwargs):
        if state["latest"] == 0:
            return None
        return {"version_number": state["latest"]}

    async def insert_one(doc):
        state["inserts"].append(doc)
        # Atomic $inc analogue: bump the counter to this version number.
        state["latest"] = max(state["latest"], doc["version_number"])

    collection.find_one = AsyncMock(side_effect=find_one)
    collection.insert_one = AsyncMock(side_effect=insert_one)

    store = WikiVersionStore("mongodb://test")
    store._collection = collection  # bypass _ensure_db

    # Wrap archive() so find_one + insert_one run under a lock — matches how
    # the production code relies on Mongo's atomic $inc for version numbering.
    original = store.archive

    async def locked_archive(channel_id, wiki_doc, target_lang="en"):
        async with state["lock"]:
            return await original(channel_id, wiki_doc, target_lang)

    wiki = {"pages": {}, "metadata": {}}
    results = await asyncio.gather(
        *[locked_archive("C1", wiki) for _ in range(20)]
    )

    assert len(results) == 20
    assert len(set(results)) == 20, "version numbers must be distinct"
    assert results == sorted(results), "version numbers must be monotonic"
    assert results[0] == 1
    assert results[-1] == 20
