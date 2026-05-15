"""Test that WikiBuilder stores wiki under the correct target_lang cache key.

Regression test for C1: builder was calling save_wiki(channel_id, wiki_dict)
without forwarding target_lang, so non-default languages always wrote to the
"en" slot.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fake_cache() -> MagicMock:
    """In-memory fake cache that records save_wiki calls by compound key."""
    store: dict[str, dict] = {}
    saved_calls: list[tuple[str, str]] = []  # (channel_id_arg, target_lang_arg)

    cache = MagicMock(name="FakeWikiCache")
    cache._store = store
    cache._saved_calls = saved_calls

    async def _save_wiki(channel_id: str, wiki_data: dict, target_lang: str = "en") -> None:
        key = f"{channel_id}:{target_lang}"
        store[key] = dict(wiki_data)
        saved_calls.append((channel_id, target_lang))

    async def _set_generation_status(**kwargs) -> None:  # noqa: ANN003
        pass

    cache.save_wiki = AsyncMock(side_effect=_save_wiki)
    cache.set_generation_status = AsyncMock(side_effect=_set_generation_status)
    return cache


def _make_fake_compiler() -> MagicMock:
    """Fake WikiCompiler that returns canned pages."""
    from beever_atlas.models.domain import WikiPage, WikiStructure

    overview = WikiPage(
        id="overview",
        slug="overview",
        title="Overview",
        content="# Overview\nTest content.",
        page_type="fixed",
    )
    canned_pages = {"overview": overview}

    compiler = MagicMock(name="FakeWikiCompiler")
    compiler.compile = AsyncMock(return_value=canned_pages)
    compiler.build_structure = MagicMock(
        return_value=WikiStructure(
            channel_id="C1",
            channel_name="test-channel",
            platform="slack",
            pages=[],
        )
    )
    return compiler


def _make_fake_gatherer_data() -> dict:
    from beever_atlas.models.domain import ChannelSummary

    summary = ChannelSummary(
        channel_id="C1",
        channel_name="test-channel",
        media_count=0,
        glossary_terms=[],
    )
    return {
        "channel_summary": summary,
        "clusters": [],
        "decisions": [],
        "media_facts": [],
        "total_facts": 1,
        "total_entities": 0,
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_wiki_uses_target_lang_key() -> None:
    """save_wiki must be called with target_lang='zh-HK', not 'en'."""
    from beever_atlas.wiki.builder import WikiBuilder

    fake_cache = _make_fake_cache()
    fake_compiler = _make_fake_compiler()
    fake_data = _make_fake_gatherer_data()

    builder = WikiBuilder(
        weaviate_store=MagicMock(),
        graph_store=MagicMock(),
        wiki_cache=fake_cache,
    )
    builder._gatherer.gather = AsyncMock(return_value=fake_data)
    builder._make_compiler = MagicMock(return_value=fake_compiler)

    with patch("beever_atlas.wiki.builder.get_llm_provider") as mock_llm:
        mock_llm.return_value.get_model_string.return_value = "test-model"
        await builder.generate_wiki(
            channel_id="C1",
            target_lang="zh-HK",
            source_lang="en",
        )

    # The cache key "C1:zh-HK" must exist
    assert "C1:zh-HK" in fake_cache._store, (
        f"Expected key 'C1:zh-HK' in cache, got keys: {list(fake_cache._store)}"
    )
    # The "en" slot must NOT have been created
    assert "C1:en" not in fake_cache._store, "Key 'C1:en' should not exist when target_lang='zh-HK'"


@pytest.mark.asyncio
async def test_concurrent_generations_serialize_per_channel() -> None:
    """Two concurrent generate_wiki calls on the same channel must serialize.

    Regression test for H1: the API layer builds a fresh WikiBuilder per
    request, so instance-level locks did not prevent overlapping runs.
    Module-level per-channel locks must serialize concurrent invocations
    even across distinct WikiBuilder instances.
    """
    import asyncio

    from beever_atlas.wiki.builder import WikiBuilder

    # Shared observation state
    concurrent_max = 0
    running = 0
    lock = asyncio.Lock()

    async def _slow_gather(_channel_id: str) -> dict:
        nonlocal running, concurrent_max
        async with lock:
            running += 1
            if running > concurrent_max:
                concurrent_max = running
        # Yield control so a truly concurrent peer would observe running > 1
        await asyncio.sleep(0.05)
        async with lock:
            running -= 1
        return _make_fake_gatherer_data()

    def _make_builder() -> WikiBuilder:
        fake_cache = _make_fake_cache()
        b = WikiBuilder(
            weaviate_store=MagicMock(),
            graph_store=MagicMock(),
            wiki_cache=fake_cache,
        )
        b._gatherer.gather = AsyncMock(side_effect=_slow_gather)
        b._make_compiler = MagicMock(return_value=_make_fake_compiler())
        return b

    builder_a = _make_builder()
    builder_b = _make_builder()

    with patch("beever_atlas.wiki.builder.get_llm_provider") as mock_llm:
        mock_llm.return_value.get_model_string.return_value = "test-model"
        await asyncio.gather(
            builder_a.generate_wiki(channel_id="C_concurrent", target_lang="en", source_lang="en"),
            builder_b.generate_wiki(
                channel_id="C_concurrent", target_lang="zh-HK", source_lang="en"
            ),
        )

    assert concurrent_max == 1, (
        f"Expected serialized runs, observed peak concurrency={concurrent_max}"
    )


@pytest.mark.asyncio
async def test_zh_hk_doc_untouched_after_en_generation() -> None:
    """A second generation with target_lang='en' must not overwrite zh-HK."""
    from beever_atlas.wiki.builder import WikiBuilder

    fake_cache = _make_fake_cache()
    fake_data = _make_fake_gatherer_data()

    # Pre-populate zh-HK slot
    fake_cache._store["C1:zh-HK"] = {"pages": {"overview": {"id": "zh-overview"}}}

    builder = WikiBuilder(
        weaviate_store=MagicMock(),
        graph_store=MagicMock(),
        wiki_cache=fake_cache,
    )
    builder._gatherer.gather = AsyncMock(return_value=fake_data)
    builder._make_compiler = MagicMock(return_value=_make_fake_compiler())

    with patch("beever_atlas.wiki.builder.get_llm_provider") as mock_llm:
        mock_llm.return_value.get_model_string.return_value = "test-model"
        await builder.generate_wiki(
            channel_id="C1",
            target_lang="en",
            source_lang="en",
        )

    # zh-HK slot is untouched
    assert fake_cache._store.get("C1:zh-HK") == {"pages": {"overview": {"id": "zh-overview"}}}, (
        "zh-HK document must not be modified by an 'en' generation"
    )

    # en slot was written
    assert "C1:en" in fake_cache._store


@pytest.mark.asyncio
async def test_seeds_wiki_pages_for_each_compiled_page() -> None:
    """After ``save_wiki``, Builder must seed ``wiki_pages`` with one
    ``persistence.WikiPage`` per compiled domain page. Closes the
    first-sync chicken-and-egg deadlock where the maintainer's
    incremental flush would forever defer because nothing wrote to
    ``wiki_pages``."""
    from beever_atlas.models.domain import WikiPage as DomainWikiPage
    from beever_atlas.wiki.builder import WikiBuilder

    fake_cache = _make_fake_cache()
    # Attach a ``_db`` handle since the seed path constructs a
    # ``WikiPageStore(db=self._cache._db)``.
    fake_cache._db = MagicMock()

    # Compiler returns three pages — overview + two topics. The seed
    # must call save_page once per page.
    compiled_pages = {
        "overview": DomainWikiPage(
            id="overview",
            slug="overview",
            title="Overview",
            content="...",
            page_type="fixed",
        ),
        "topic-alpha": DomainWikiPage(
            id="topic-alpha",
            slug="alpha",
            title="Alpha",
            content="...",
            page_type="topic",
        ),
        "topic-beta": DomainWikiPage(
            id="topic-beta",
            slug="beta",
            title="Beta",
            content="...",
            page_type="topic",
        ),
    }
    fake_compiler = _make_fake_compiler()
    fake_compiler.compile = AsyncMock(return_value=compiled_pages)

    saved_page_ids: list[str] = []

    class _FakePageStore:
        def __init__(self, db: object) -> None:  # noqa: ARG002 — matches real ctor
            pass

        async def save_page(self, page: object) -> None:
            saved_page_ids.append(getattr(page, "page_id", "?"))

    builder = WikiBuilder(
        weaviate_store=MagicMock(),
        graph_store=MagicMock(),
        wiki_cache=fake_cache,
    )
    builder._gatherer.gather = AsyncMock(return_value=_make_fake_gatherer_data())
    builder._make_compiler = MagicMock(return_value=fake_compiler)

    with (
        patch("beever_atlas.wiki.builder.get_llm_provider") as mock_llm,
        patch("beever_atlas.wiki.page_store.WikiPageStore", _FakePageStore),
    ):
        mock_llm.return_value.get_model_string.return_value = "test-model"
        await builder.generate_wiki(channel_id="C1", target_lang="en", source_lang="en")

    assert set(saved_page_ids) == {"overview", "topic-alpha", "topic-beta"}, (
        f"Expected seed for every compiled page; got {saved_page_ids}"
    )


@pytest.mark.asyncio
async def test_seed_failure_for_one_page_does_not_kill_build() -> None:
    """The per-page try/except inside the seed loop must isolate
    failures so a single misbehaving row never wedges the whole build.
    The legacy ``save_wiki`` write still has to land (UI keeps working)
    and the other pages still seed."""
    from beever_atlas.models.domain import WikiPage as DomainWikiPage
    from beever_atlas.wiki.builder import WikiBuilder

    fake_cache = _make_fake_cache()
    fake_cache._db = MagicMock()

    compiled_pages = {
        "overview": DomainWikiPage(
            id="overview",
            slug="overview",
            title="Overview",
            content="...",
            page_type="fixed",
        ),
        "topic-explodes": DomainWikiPage(
            id="topic-explodes",
            slug="kaboom",
            title="Kaboom",
            content="...",
            page_type="topic",
        ),
        "topic-ok": DomainWikiPage(
            id="topic-ok",
            slug="ok",
            title="OK",
            content="...",
            page_type="topic",
        ),
    }
    fake_compiler = _make_fake_compiler()
    fake_compiler.compile = AsyncMock(return_value=compiled_pages)

    saved_page_ids: list[str] = []

    class _FlakyPageStore:
        def __init__(self, db: object) -> None:  # noqa: ARG002
            pass

        async def save_page(self, page: object) -> None:
            pid = getattr(page, "page_id", "?")
            if pid == "topic-explodes":
                raise RuntimeError("simulated mongo write hiccup")
            saved_page_ids.append(pid)

    builder = WikiBuilder(
        weaviate_store=MagicMock(),
        graph_store=MagicMock(),
        wiki_cache=fake_cache,
    )
    builder._gatherer.gather = AsyncMock(return_value=_make_fake_gatherer_data())
    builder._make_compiler = MagicMock(return_value=fake_compiler)

    with (
        patch("beever_atlas.wiki.builder.get_llm_provider") as mock_llm,
        patch("beever_atlas.wiki.page_store.WikiPageStore", _FlakyPageStore),
    ):
        mock_llm.return_value.get_model_string.return_value = "test-model"
        # MUST NOT raise — per-page error is isolated.
        await builder.generate_wiki(channel_id="C1", target_lang="en", source_lang="en")

    # Other pages still seeded
    assert set(saved_page_ids) == {"overview", "topic-ok"}
    # And the build's primary surface (wiki_cache) was written
    assert "C1:en" in fake_cache._store
