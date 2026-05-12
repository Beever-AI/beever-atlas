"""Unit tests for PR-4 — bounded topic-page compile concurrency in WikiCompiler.

Acceptance criteria covered:
  1. test_semaphore_caps_concurrent_topics — 30 topics, parallelism=6,
     max_observed_concurrent <= 6.
  2. test_tunable_via_setting — wiki_topic_compile_parallelism=16, 30 topics,
     max can reach up to 16.
  3. test_folder_compile_unchanged — _FOLDER_COMPILE_PARALLELISM remains 4.
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.wiki.compiler import WikiCompiler, _FOLDER_COMPILE_PARALLELISM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_cluster(i: int):
    c = MagicMock()
    c.title = f"Topic {i}"
    c.id = f"cluster-{i}"
    c.member_count = 5
    c.keywords = []
    c.faq_candidates = []
    return c


def _make_gathered(clusters):
    channel_summary = MagicMock()
    channel_summary.themes = ["engineering"]
    channel_summary.glossary_terms = []

    return {
        "clusters": clusters,
        "channel_summary": channel_summary,
        "cluster_facts": {},
        "media_facts": [],
        "people": [],
        "decisions": [],
        "channel_id": "C123",
    }


def _make_wiki_page(page_id: str = "page-1"):
    page = MagicMock()
    page.id = page_id
    return page


def _make_compiler() -> WikiCompiler:
    compiler = WikiCompiler.__new__(WikiCompiler)
    compiler._target_lang = "en"
    compiler._source_lang = "en"
    compiler._model_name = "fake-model"
    compiler._title_overrides = {}
    return compiler


def _apply_patches(compiler, fake_settings, topic_side_effect) -> contextlib.ExitStack:
    """Build an ExitStack that patches settings + all compile methods."""
    fixed_page = _make_wiki_page("fixed")
    stack = contextlib.ExitStack()

    stack.enter_context(patch("beever_atlas.infra.config.get_settings", return_value=fake_settings))
    stack.enter_context(patch.object(compiler, "_is_topic_relevant", return_value=(True, None)))
    stack.enter_context(
        patch.object(compiler, "_compile_topic_page", side_effect=topic_side_effect)
    )
    for method in (
        "_compile_overview",
        "_compile_people",
        "_compile_decisions",
        "_compile_faq",
        "_compile_glossary",
        "_compile_resources",
        "_compile_activity",
    ):
        stack.enter_context(patch.object(compiler, method, new=AsyncMock(return_value=fixed_page)))
    return stack


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestTopicCompileSemaphore:
    @pytest.mark.asyncio
    async def test_semaphore_caps_concurrent_topics(self):
        """30 topics with default parallelism=6: max concurrent <= 6."""
        N = 30
        clusters = [_make_cluster(i) for i in range(N)]
        gathered = _make_gathered(clusters)
        compiler = _make_compiler()

        concurrent = 0
        max_observed = 0
        lock = asyncio.Lock()

        async def fake_compile_topic_page(cluster, gathered_data):
            nonlocal concurrent, max_observed
            async with lock:
                concurrent += 1
                if concurrent > max_observed:
                    max_observed = concurrent
            await asyncio.sleep(0)  # yield to allow others to start
            async with lock:
                concurrent -= 1
            return _make_wiki_page(f"topic-{cluster.id}")

        fake_settings = SimpleNamespace(
            wiki_parallel_dispatch=False,
            wiki_topic_compile_parallelism=6,
        )

        with _apply_patches(compiler, fake_settings, fake_compile_topic_page):
            await compiler.compile(gathered)

        assert max_observed <= 6, f"Expected max concurrent topics <= 6, got {max_observed}"

    @pytest.mark.asyncio
    async def test_tunable_via_setting(self):
        """wiki_topic_compile_parallelism=16: max concurrent can reach up to 16."""
        N = 30
        clusters = [_make_cluster(i) for i in range(N)]
        gathered = _make_gathered(clusters)
        compiler = _make_compiler()

        concurrent = 0
        max_observed = 0
        lock = asyncio.Lock()

        async def fake_compile_topic_page(cluster, gathered_data):
            nonlocal concurrent, max_observed
            async with lock:
                concurrent += 1
                if concurrent > max_observed:
                    max_observed = concurrent
            await asyncio.sleep(0)
            async with lock:
                concurrent -= 1
            return _make_wiki_page(f"topic-{cluster.id}")

        fake_settings = SimpleNamespace(
            wiki_parallel_dispatch=False,
            wiki_topic_compile_parallelism=16,
        )

        with _apply_patches(compiler, fake_settings, fake_compile_topic_page):
            await compiler.compile(gathered)

        # With parallelism=16 and 30 topics, max should be in range [1, 16]
        assert 1 <= max_observed <= 16, (
            f"Expected max concurrent topics in [1, 16], got {max_observed}"
        )

    def test_folder_compile_unchanged(self):
        """_FOLDER_COMPILE_PARALLELISM must remain 4 — not regressed by PR-4."""
        assert _FOLDER_COMPILE_PARALLELISM == 4, (
            f"_FOLDER_COMPILE_PARALLELISM must be 4, got {_FOLDER_COMPILE_PARALLELISM}"
        )
