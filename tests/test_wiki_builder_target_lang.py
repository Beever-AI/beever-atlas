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
    assert "C1:en" not in fake_cache._store, (
        "Key 'C1:en' should not exist when target_lang='zh-HK'"
    )


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
            builder_a.generate_wiki(
                channel_id="C_concurrent", target_lang="en", source_lang="en"
            ),
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
    assert fake_cache._store.get("C1:zh-HK") == {
        "pages": {"overview": {"id": "zh-overview"}}
    }, "zh-HK document must not be modified by an 'en' generation"

    # en slot was written
    assert "C1:en" in fake_cache._store
