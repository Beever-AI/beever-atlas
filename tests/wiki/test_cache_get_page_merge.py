"""Regression tests for ``WikiCache.get_page`` two-store merge logic.

Locks in the behavior introduced after the first-sync seed fix: when
``PER_PAGE_WIKI`` is on AND ``WikiPageStore`` has a seeded row, the
returned dict must overlay the legacy ``wiki_cache`` blob's render-only
fields (content, modules, narrative_sections, …) on top of the
persistence row's maintenance metadata (kind, is_dirty, version,
last_facts_seen, …). Pre-fix the seed shadowed the cache blob and the
frontend received a page without ``content`` → TopicPage.tsx crashed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.wiki.cache import WikiCache


def _persistence_row() -> dict[str, Any]:
    """A minimal persistence.WikiPage dict as returned by save_page →
    model_dump. Maintenance metadata only; no rendered content."""
    return {
        "channel_id": "C1",
        "target_lang": "en",
        "page_id": "topic-test",
        "title": "Test Topic",
        "slug": "test-topic",
        "kind": "topic",
        "version": 4,
        "is_dirty": True,
        "last_facts_seen": ["f1", "f2", "f3"],
        "curation_mode": "auto",
        "sections": [],
        "tensions": [],
    }


def _cache_row() -> dict[str, Any]:
    """A legacy wiki_cache.pages[page_id] dict as written by Builder
    via ``save_wiki``. Carries the render-only surface the frontend
    actually displays."""
    return {
        "page_id": "topic-test",
        "title": "Test Topic — Cache Version",
        "page_type": "topic",
        "content": "## Section\n\nFull rendered markdown body, 2k chars.",
        "modules": [{"id": "hero_summary", "data": {"tldr": "headline"}}],
        "narrative_sections": [{"anchor": "intro", "paragraphs": []}],
        "summary": "One-liner for cards.",
        "memory_count": 8,
        "citations": [{"id": "[1]", "fact_id": "f1"}],
        "children": [],
        "section_number": "2.3",
        "last_updated": datetime.now(tz=UTC).isoformat(),
    }


def _make_cache_with_stores(
    *, per_page: dict | None, cache_blob: dict | None, per_page_wiki: bool = True
) -> WikiCache:
    """Build a WikiCache wired to fake page_store + collection. Skips
    ``__init__`` so we don't need a real MongoDB URI."""
    cache = WikiCache.__new__(WikiCache)
    cache._db = MagicMock()
    cache._ensure_db = AsyncMock()

    # Legacy ``wiki_cache.pages[<page_id>]`` lookup.
    fake_collection = MagicMock()
    if cache_blob is None:
        fake_collection.find_one = AsyncMock(return_value=None)
    else:
        fake_collection.find_one = AsyncMock(
            return_value={"pages": {cache_blob["page_id"]: cache_blob}}
        )
    cache._collection = fake_collection

    # Settings flag — controls whether the per-page store path runs.
    settings_stub = SimpleNamespace(per_page_wiki=per_page_wiki, default_target_language="en")
    settings_patch = patch(
        "beever_atlas.wiki.cache.get_settings",
        return_value=settings_stub,
    )
    settings_patch.start()

    # PageStore stub — returns a Pydantic-ish object with model_dump.
    page_obj = None
    if per_page is not None:
        page_obj = MagicMock()
        page_obj.model_dump = MagicMock(return_value=per_page)
    page_store_stub = MagicMock()
    page_store_stub.get_page = AsyncMock(return_value=page_obj)
    page_store_patch = patch(
        "beever_atlas.wiki.page_store.WikiPageStore",
        return_value=page_store_stub,
    )
    page_store_patch.start()

    # Track patchers so the test can stop them in teardown — we attach
    # them onto the cache so each test does ``cache._patches[i].stop()``.
    cache._test_patches = [settings_patch, page_store_patch]  # type: ignore[attr-defined]
    return cache


def _teardown(cache: WikiCache) -> None:
    for p in getattr(cache, "_test_patches", []):
        p.stop()


@pytest.mark.asyncio
async def test_merge_overlays_render_only_fields_from_cache() -> None:
    """When both stores have an entry the cache's render-only fields
    must appear in the result. Frontend depends on ``content``."""
    cache = _make_cache_with_stores(per_page=_persistence_row(), cache_blob=_cache_row())
    try:
        result = await cache.get_page("C1", "topic-test", target_lang="en")
        assert result is not None
        assert result["content"].startswith("## Section")
        assert result["modules"][0]["id"] == "hero_summary"
        assert result["narrative_sections"][0]["anchor"] == "intro"
        assert result["summary"] == "One-liner for cards."
        assert result["memory_count"] == 8
        assert result["citations"][0]["fact_id"] == "f1"
    finally:
        _teardown(cache)


@pytest.mark.asyncio
async def test_merge_preserves_persistence_metadata() -> None:
    """Persistence-only fields (kind, is_dirty, version, last_facts_seen,
    curation_mode) must survive the overlay. The maintainer writes
    these; a future ``wiki_cache`` blob that accidentally included a
    ``kind`` or ``version`` key must NOT shadow them."""
    pers = _persistence_row()
    cache_blob = _cache_row()
    # Simulate a hostile cache blob that carries metadata keys it
    # shouldn't (forward-compat defense).
    cache_blob["kind"] = "WRONG_KIND"
    cache_blob["version"] = 99
    cache_blob["is_dirty"] = False
    cache_blob["last_facts_seen"] = ["DROPPED"]
    cache_blob["curation_mode"] = "WRONG"

    cache = _make_cache_with_stores(per_page=pers, cache_blob=cache_blob)
    try:
        result = await cache.get_page("C1", "topic-test", target_lang="en")
        assert result is not None
        # Persistence keys win.
        assert result["kind"] == "topic"
        assert result["version"] == 4
        assert result["is_dirty"] is True
        assert result["last_facts_seen"] == ["f1", "f2", "f3"]
        assert result["curation_mode"] == "auto"
    finally:
        _teardown(cache)


@pytest.mark.asyncio
async def test_per_page_only_returned_as_is() -> None:
    """When only the persistence row exists the result is the
    persistence dict unchanged (no render-only overlay possible)."""
    cache = _make_cache_with_stores(per_page=_persistence_row(), cache_blob=None)
    try:
        result = await cache.get_page("C1", "topic-test", target_lang="en")
        assert result is not None
        assert result["page_id"] == "topic-test"
        assert result["kind"] == "topic"
        assert "content" not in result  # nothing to overlay
    finally:
        _teardown(cache)


@pytest.mark.asyncio
async def test_cache_only_returned_as_is() -> None:
    """When only the legacy cache blob exists (no seed yet, or
    PER_PAGE_WIKI off) the result is the cache dict unchanged. This is
    the pre-fix shape that channels prior to the seed deployment rely
    on for backward compat."""
    cache = _make_cache_with_stores(per_page=None, cache_blob=_cache_row())
    try:
        result = await cache.get_page("C1", "topic-test", target_lang="en")
        assert result is not None
        assert result["page_id"] == "topic-test"
        assert result["content"].startswith("## Section")
    finally:
        _teardown(cache)


@pytest.mark.asyncio
async def test_neither_store_returns_none() -> None:
    """Missing in both → None (the 404 path)."""
    cache = _make_cache_with_stores(per_page=None, cache_blob=None)
    try:
        result = await cache.get_page("C1", "topic-test", target_lang="en")
        assert result is None
    finally:
        _teardown(cache)


@pytest.mark.asyncio
async def test_per_page_wiki_disabled_uses_cache_only() -> None:
    """``per_page_wiki=False`` short-circuits the persistence lookup so
    the legacy behavior (cache-only) is fully preserved for operators
    who roll back the flag."""
    cache = _make_cache_with_stores(
        per_page=_persistence_row(),
        cache_blob=_cache_row(),
        per_page_wiki=False,
    )
    try:
        result = await cache.get_page("C1", "topic-test", target_lang="en")
        assert result is not None
        # Only the cache blob shape — no persistence keys layered in.
        assert "kind" not in result or result.get("kind") != "topic"
        assert result["content"].startswith("## Section")
    finally:
        _teardown(cache)
