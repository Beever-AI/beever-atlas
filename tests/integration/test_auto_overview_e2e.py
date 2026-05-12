"""End-to-end integration test for the auto-overview subscriber
(sync-pipeline-feedback-and-auto-wiki §3.10).

Verifies the full ``ExtractionWorker.on_extraction_done`` →
``AutoOverviewSubscriber.on_extraction_done`` →
``WikiBuilder.refresh_wiki`` chain by mocking at the store boundary
plus the builder, NOT the subscriber itself. Confirms the subscriber
is correctly registered as a ``subscribe_extraction_done`` callback
and the worker's ``_emit_extraction_done`` reaches it with the right
arguments.

Mocks Mongo and the LLM-backed ``WikiBuilder`` so the test runs in a
few hundred milliseconds without external dependencies. A real e2e
test (Weaviate + LLM) would be expensive to run on every CI run; this
test covers the wiring contract.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.services.auto_overview_subscriber import (
    AutoOverviewSubscriber,
)
from beever_atlas.services.extraction_worker import ExtractionWorker


@pytest.mark.asyncio
async def test_e2e_first_sync_completes_overview_auto_generates() -> None:
    """Fresh-install path:

    1. Inject 10 channel_messages with extraction_status=done.
    2. Fire ``on_extraction_done(channel_id, [fact_ids...])`` on the worker.
    3. Assert the subscriber's mocked generator was called once with the
       right channel_id + language.
    """
    channel_id = "C-fresh"

    class _FakeMongo:
        async def count_channel_messages_by_status(self, _channel_id: str) -> dict[str, int]:
            return {"pending": 0, "extracting": 0, "done": 10, "failed": 0}

        @property
        def db(self) -> Any:
            class _WikiPagesCollection:
                async def find_one(_self, query, projection=None):  # noqa: ANN001
                    return None  # no overview exists yet

            class _DB:
                def __getitem__(_self, name):  # noqa: ANN001
                    if name == "wiki_pages":
                        return _WikiPagesCollection()
                    raise KeyError(name)

            return _DB()

    class _FakeStores:
        mongodb = _FakeMongo()

    fake_stores = _FakeStores()

    # Mock the generator (the boundary where the subscriber would call
    # WikiBuilder.refresh_wiki). Capturing here rather than mocking
    # WikiBuilder lets us assert without monkey-patching internals of
    # the wiki module tree.
    generator = AsyncMock()
    sub = AutoOverviewSubscriber(
        min_facts_threshold=5,
        feature_flag_resolver=AsyncMock(return_value=True),
        language_resolver=AsyncMock(return_value="en"),
        generator=generator,
    )
    sub._get_stores = lambda: fake_stores  # type: ignore[method-assign]

    # Wire into a real ExtractionWorker — proves the subscription
    # contract works end-to-end (signature + dispatch).
    worker = ExtractionWorker()

    def _on_done_sync(ch: str, fids: list[str]) -> None:
        # The lifespan in production wraps this in asyncio.create_task
        # so the worker batch loop never blocks on the LLM build. We
        # mirror that pattern here so the test can ``gather`` to wait
        # for the subscriber's body to actually complete (otherwise the
        # generator assertion would race the dispatch).
        asyncio.create_task(sub.on_extraction_done(ch, fids))

    worker.subscribe_extraction_done(_on_done_sync)

    # Use the public emit method by triggering a dispatch through the
    # subscriber list — the worker's _emit_extraction_done is private
    # but the public surface is subscribe_extraction_done + the worker
    # firing it internally. For this test we use the subscriber list
    # directly to mirror what the worker does on a real tick.
    await worker._emit_extraction_done(channel_id, ["f1", "f2", "f3"])

    # Wait for the fire-and-forget child task to settle.
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    generator.assert_awaited_once_with(channel_id, "en")


@pytest.mark.asyncio
async def test_e2e_subsequent_sync_does_not_regenerate() -> None:
    """Second-sync invariant:

    When an overview already exists, the worker emits another
    ``on_extraction_done`` and the subscriber must no-op. The manual
    Generate button remains the only path to regeneration.
    """
    channel_id = "C-existing"

    class _FakeMongo:
        async def count_channel_messages_by_status(self, _channel_id: str) -> dict[str, int]:
            return {"pending": 0, "extracting": 0, "done": 100, "failed": 0}

        @property
        def db(self) -> Any:
            class _WikiPagesCollection:
                async def find_one(_self, query, projection=None):  # noqa: ANN001
                    return {"_id": "existing-overview"}  # overview EXISTS

            class _DB:
                def __getitem__(_self, name):  # noqa: ANN001
                    if name == "wiki_pages":
                        return _WikiPagesCollection()
                    raise KeyError(name)

            return _DB()

    class _FakeStores:
        mongodb = _FakeMongo()

    fake_stores = _FakeStores()
    generator = AsyncMock()
    sub = AutoOverviewSubscriber(
        feature_flag_resolver=AsyncMock(return_value=True),
        language_resolver=AsyncMock(return_value="en"),
        generator=generator,
    )
    sub._get_stores = lambda: fake_stores  # type: ignore[method-assign]

    worker = ExtractionWorker()

    def _on_done_sync(ch: str, fids: list[str]) -> None:
        asyncio.create_task(sub.on_extraction_done(ch, fids))

    worker.subscribe_extraction_done(_on_done_sync)
    await worker._emit_extraction_done(channel_id, ["f100", "f101"])

    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    generator.assert_not_awaited()
