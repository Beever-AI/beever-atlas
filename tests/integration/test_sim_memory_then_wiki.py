"""Sim coverage for the memory-then-wiki-pipeline-realignment change.

Locks in the structural invariant: the WikiMaintainer's per-page LLM
rewrite fires AT MOST ONCE per affected page per channel during a
bulk sync — not once per batch.

The accumulator-vs-terminal split is the central guarantee:

  ExtractionWorker
    ├─ memory_changed(channel_id, fact_ids)  ◄── accumulator (per-batch)
    │     └─► WikiMaintainer.on_memory_changed
    │             └─► routes facts → enqueues into wiki_dirty_queue
    │                 (NO debounce, NO LLM call)
    │
    └─ memory_settled(channel_id)            ◄── terminal (queue drains)
          └─► WikiMaintainer.on_memory_settled
                  └─► schedules one debounced flush per channel
                      └─► single apply_update per affected page
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from beever_atlas.services.wiki_maintainer import WikiMaintainer


@pytest.mark.asyncio
async def test_on_memory_changed_routes_without_scheduling_flush() -> None:
    """``on_memory_changed`` enqueues to wiki_dirty_queue and writes
    through to the in-memory dirty set — but MUST NOT schedule a flush."""

    page_store_mock = type("PS", (), {
        "list_pages": lambda *a, **kw: __import__("asyncio").sleep(0),
    })()

    maint = WikiMaintainer(page_store=page_store_mock)  # type: ignore[arg-type]

    async def _stub_route(channel_id, fact_ids, *, target_lang="en"):
        return {"topic:gpu": list(fact_ids)}

    maint._route_facts_to_pages = _stub_route  # type: ignore[method-assign]

    # Track whether _ensure_flush_scheduled is called (it MUST NOT be).
    scheduled_calls = 0

    def _track_schedule(*args, **kwargs):
        nonlocal scheduled_calls
        scheduled_calls += 1

    maint._ensure_flush_scheduled = _track_schedule  # type: ignore[method-assign]

    # The maintainer dispatches an enqueue_dirty call into the stores
    # singleton — patch it out so we don't need a live Mongo.
    enqueue_calls: list[tuple] = []

    class _FakeStores:
        class mongodb:
            @staticmethod
            async def enqueue_dirty(channel_id, page_id, fact_ids):
                enqueue_calls.append((channel_id, page_id, tuple(fact_ids)))

    with patch("beever_atlas.stores.get_stores", lambda: _FakeStores()):
        await maint.on_memory_changed("C1", ["f1", "f2"], target_lang="en")

    # The accumulator fired (enqueue), but no flush was scheduled.
    assert ("C1", "topic:gpu", ("f1", "f2")) in enqueue_calls
    assert scheduled_calls == 0, (
        f"on_memory_changed must not schedule a flush; got {scheduled_calls} calls"
    )
    # Mirror in legacy in-memory dirty set for the deprecation window.
    assert ("C1", "topic:gpu") in maint._dirty


@pytest.mark.asyncio
async def test_on_memory_settled_schedules_debounce() -> None:
    """``on_memory_settled`` MUST schedule exactly one debounced flush
    when not in manual mode."""

    maint = WikiMaintainer(page_store=None, mode="auto")  # type: ignore[arg-type]
    maint._debounce_seconds_override = 60  # non-zero debounce

    schedule_count = 0

    def _track_schedule(debounce, *, target_lang="en"):
        nonlocal schedule_count
        schedule_count += 1

    maint._ensure_flush_scheduled = _track_schedule  # type: ignore[method-assign]

    result = await maint.on_memory_settled("C1", target_lang="en")
    assert result["scheduled"] == 1
    assert schedule_count == 1


@pytest.mark.asyncio
async def test_on_memory_settled_manual_mode_skips_flush() -> None:
    """Manual mode defers all flushes to the operator's button click —
    on_memory_settled becomes a no-op."""

    maint = WikiMaintainer(page_store=None, mode="manual")  # type: ignore[arg-type]
    scheduled_calls = 0

    def _track_schedule(*args, **kwargs):
        nonlocal scheduled_calls
        scheduled_calls += 1

    maint._ensure_flush_scheduled = _track_schedule  # type: ignore[method-assign]

    result = await maint.on_memory_settled("C1", target_lang="en")
    assert result["scheduled"] == 0
    assert scheduled_calls == 0


@pytest.mark.asyncio
async def test_bulk_burst_fires_one_flush_not_N() -> None:
    """Simulate a bulk sync: 5 ``memory_changed`` events touching the
    same page, then ONE ``memory_settled`` event. Only one flush
    schedule MUST fire (not N)."""

    page_store_mock = type("PS", (), {
        "list_pages": lambda *a, **kw: __import__("asyncio").sleep(0),
    })()
    maint = WikiMaintainer(page_store=page_store_mock, mode="auto")  # type: ignore[arg-type]
    maint._debounce_seconds_override = 60

    async def _stub_route(channel_id, fact_ids, *, target_lang="en"):
        return {"topic:gpu": list(fact_ids)}

    maint._route_facts_to_pages = _stub_route  # type: ignore[method-assign]

    schedule_count = 0

    def _track_schedule(*args, **kwargs):
        nonlocal schedule_count
        schedule_count += 1

    maint._ensure_flush_scheduled = _track_schedule  # type: ignore[method-assign]

    class _FakeStores:
        class mongodb:
            @staticmethod
            async def enqueue_dirty(channel_id, page_id, fact_ids):
                pass

    with patch("beever_atlas.stores.get_stores", lambda: _FakeStores()):
        # 5 batches' worth of memory_changed events.
        for i in range(5):
            await maint.on_memory_changed("C1", [f"f{i}"], target_lang="en")
        # NO flush scheduled yet (accumulator only).
        assert schedule_count == 0

        # Single memory_settled fires when the queue drains.
        await maint.on_memory_settled("C1", target_lang="en")

    assert schedule_count == 1, (
        "Bulk sync must produce exactly 1 flush schedule, not 1 per batch"
    )
