"""Unit tests for the WikiMaintainer per-page debounce window
(sync-pipeline-feedback-and-auto-wiki §B3, design D3).

These tests exercise the in-memory dirty-set + debounced flush path that
collapses N extraction events touching the same page into a single LLM
rewrite. They use a short debounce window (0.1s) so the suite stays fast
while still going through the real ``asyncio.sleep`` path.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.services.wiki_maintainer import WikiMaintainer


def _make_recording_maintainer(
    routing: dict[str, Any] | None = None,
    *,
    debounce_seconds: float,
) -> tuple[WikiMaintainer, list[tuple[str, str, list[str]]]]:
    """Build a maintainer that records every ``_rewrite_page`` invocation.

    ``routing`` controls how ``_route_facts_to_pages`` maps fact_ids →
    page_ids. When None, every event routes to the single page
    ``"page:test"``. The recorded calls are returned as a list of
    ``(channel_id, page_id, fact_ids)`` tuples in invocation order.
    """
    rewrite_calls: list[tuple[str, str, list[str]]] = []

    class _DebounceFakeMaintainer(WikiMaintainer):
        async def _rewrite_page(
            self,
            channel_id: str,
            page_id: str,
            fact_ids: list[str],
            *,
            target_lang: str = "en",
        ) -> bool:
            rewrite_calls.append((channel_id, page_id, list(fact_ids)))
            return True

        async def _route_facts_to_pages(
            self,
            channel_id: str,
            fact_ids: list[str],
            *,
            target_lang: str = "en",
        ) -> dict[str, list[str]]:
            if routing is not None:
                return {page_id: list(fids) for page_id, fids in routing.items()}
            return {"page:test": list(fact_ids)}

        async def _record_merge_proposals(self, **_kwargs: Any) -> None:
            return None

    page_store = AsyncMock()
    maintainer = _DebounceFakeMaintainer(
        page_store=page_store,
        debounce_seconds=debounce_seconds,
        mode="auto",
    )
    return maintainer, rewrite_calls


@pytest.mark.asyncio
async def test_8_events_same_page_collapse_to_one_rewrite() -> None:
    """8 extraction events touching the same page within the debounce
    window must result in exactly one rewrite call carrying all 8
    events' facts.

    This is the smoking-gun regression test for §B3: live evidence
    showed the maintainer issuing 400 LLM calls in an hour because
    every extraction event triggered a synchronous rewrite. The
    debounce path collapses bursts within the window into one call.
    """
    maintainer, rewrite_calls = _make_recording_maintainer(debounce_seconds=0.1)

    for i in range(8):
        await maintainer.on_extraction_done("ch1", [f"f{i}"], mode="auto")

    # Wait for debounce + flush. The window is 0.1s; 0.3s gives the
    # flush task time to run without flaking on slow CI.
    await asyncio.sleep(0.3)

    assert len(rewrite_calls) == 1, (
        f"Expected 1 rewrite, got {len(rewrite_calls)}: {rewrite_calls!r}"
    )
    ch, page, facts = rewrite_calls[0]
    assert ch == "ch1"
    assert page == "page:test"
    assert set(facts) == {f"f{i}" for i in range(8)}


@pytest.mark.asyncio
async def test_events_different_pages_same_window_yield_n_rewrites() -> None:
    """Events touching different pages within the same window must result
    in one rewrite per page, all in the same flush cycle."""
    rewrite_calls: list[tuple[str, str, list[str]]] = []

    class _MultiPageFakeMaintainer(WikiMaintainer):
        async def _rewrite_page(
            self,
            channel_id: str,
            page_id: str,
            fact_ids: list[str],
            *,
            target_lang: str = "en",
        ) -> bool:
            rewrite_calls.append((channel_id, page_id, list(fact_ids)))
            return True

        async def _route_facts_to_pages(
            self,
            channel_id: str,
            fact_ids: list[str],
            *,
            target_lang: str = "en",
        ) -> dict[str, list[str]]:
            # Each fact maps to its own page so 5 events → 5 distinct
            # dirty entries → 5 rewrites in one flush.
            return {f"page:{fact_ids[0]}": list(fact_ids)}

        async def _record_merge_proposals(self, **_kwargs: Any) -> None:
            return None

    maintainer = _MultiPageFakeMaintainer(
        page_store=AsyncMock(),
        debounce_seconds=0.1,
        mode="auto",
    )

    for i in range(5):
        await maintainer.on_extraction_done("ch1", [f"f{i}"], mode="auto")

    await asyncio.sleep(0.3)

    assert len(rewrite_calls) == 5
    pages = {p for _, p, _ in rewrite_calls}
    assert pages == {f"page:f{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_idempotent_flush_scheduling_does_not_spawn_duplicate_tasks() -> None:
    """Subsequent events within an active window MUST NOT spawn a
    second flush task; the dirty-set is the only state that grows."""
    maintainer, rewrite_calls = _make_recording_maintainer(debounce_seconds=0.1)

    # Fire the first event — schedules a task.
    await maintainer.on_extraction_done("ch1", ["f1"], mode="auto")
    first_task = maintainer._flush_task
    assert first_task is not None and not first_task.done()

    # Fire 4 more events while the task is still sleeping. Each MUST
    # reuse the existing task — flushed by the same wakeup.
    for i in range(2, 6):
        await maintainer.on_extraction_done("ch1", [f"f{i}"], mode="auto")
        assert maintainer._flush_task is first_task, (
            "Idempotency violated: a new flush task was spawned while "
            "an existing task was still in flight."
        )

    await asyncio.sleep(0.3)
    # All 5 events collapsed to exactly one rewrite carrying all facts.
    assert len(rewrite_calls) == 1
    _, _, facts = rewrite_calls[0]
    assert set(facts) == {f"f{i}" for i in range(1, 6)}


@pytest.mark.asyncio
async def test_per_page_failure_does_not_sink_other_pages() -> None:
    """One page raising during ``_rewrite_page`` must not stop other
    pages in the same flush from being rewritten — matches the per-page
    isolation contract of the synchronous mode."""
    rewrite_calls: list[tuple[str, str, list[str]]] = []

    class _FlakyFakeMaintainer(WikiMaintainer):
        async def _rewrite_page(
            self,
            channel_id: str,
            page_id: str,
            fact_ids: list[str],
            *,
            target_lang: str = "en",
        ) -> bool:
            rewrite_calls.append((channel_id, page_id, list(fact_ids)))
            if page_id == "page:f1":
                raise RuntimeError("flaky page write")
            return True

        async def _route_facts_to_pages(
            self,
            channel_id: str,
            fact_ids: list[str],
            *,
            target_lang: str = "en",
        ) -> dict[str, list[str]]:
            return {f"page:{fact_ids[0]}": list(fact_ids)}

        async def _record_merge_proposals(self, **_kwargs: Any) -> None:
            return None

    maintainer = _FlakyFakeMaintainer(
        page_store=AsyncMock(),
        debounce_seconds=0.1,
        mode="auto",
    )

    for i in range(3):
        await maintainer.on_extraction_done("ch1", [f"f{i}"], mode="auto")

    await asyncio.sleep(0.3)

    # All 3 pages were attempted; the bad one logged but the other 2
    # still hit the recorder.
    pages_attempted = {p for _, p, _ in rewrite_calls}
    assert pages_attempted == {"page:f0", "page:f1", "page:f2"}


@pytest.mark.asyncio
async def test_zero_debounce_flushes_inline_for_synchronous_counters() -> None:
    """``debounce_seconds=0`` flushes inline so the synchronous
    ``rewritten`` counter reflects the per-page work — the contract
    relied on by existing tests in ``test_wiki_maintainer.py``."""
    maintainer, rewrite_calls = _make_recording_maintainer(debounce_seconds=0)

    counters = await maintainer.on_extraction_done("ch1", ["f1"], mode="auto")

    # Inline flush — no asyncio.sleep needed.
    assert counters["rewritten"] == 1
    assert len(rewrite_calls) == 1


@pytest.mark.asyncio
async def test_subsequent_event_after_flush_completes_schedules_new_task() -> None:
    """After a flush completes, a fresh event must spawn a NEW flush
    task — there is no ``always-running`` loop, so the next event
    reschedules from scratch."""
    maintainer, rewrite_calls = _make_recording_maintainer(debounce_seconds=0.1)

    await maintainer.on_extraction_done("ch1", ["f1"], mode="auto")
    first_task = maintainer._flush_task
    await asyncio.sleep(0.3)
    assert first_task is not None and first_task.done()
    assert len(rewrite_calls) == 1

    # New event after the first flush completed → new task scheduled.
    await maintainer.on_extraction_done("ch1", ["f2"], mode="auto")
    second_task = maintainer._flush_task
    assert second_task is not None
    assert second_task is not first_task

    await asyncio.sleep(0.3)
    assert len(rewrite_calls) == 2
    _, _, facts_second = rewrite_calls[1]
    assert facts_second == ["f2"]


@pytest.mark.asyncio
async def test_manual_mode_bypasses_debounce_entirely() -> None:
    """``mode="manual"`` must hit the synchronous mark_dirty path without
    touching the dirty-set or scheduling a flush task."""
    page_store = AsyncMock()
    page_store.mark_dirty = AsyncMock(return_value=1)

    class _ManualFakeMaintainer(WikiMaintainer):
        async def _route_facts_to_pages(
            self,
            channel_id: str,
            fact_ids: list[str],
            *,
            target_lang: str = "en",
        ) -> dict[str, list[str]]:
            return {"page:test": list(fact_ids)}

        async def _record_merge_proposals(self, **_kwargs: Any) -> None:
            return None

    # Construct WITHOUT a mode override — the per-call ``mode="manual"``
    # argument should win.
    maintainer = _ManualFakeMaintainer(
        page_store=page_store,
        debounce_seconds=60,  # high default — would flake the test if hit
    )

    counters = await maintainer.on_extraction_done("ch1", ["f1"], mode="manual")

    page_store.mark_dirty.assert_awaited_once()
    assert counters["marked_dirty"] == 1
    # No flush task scheduled.
    assert maintainer._flush_task is None
    # No dirty-set entries either.
    assert maintainer._dirty == {}


def test_resolve_debounce_seconds_uses_constructor_override() -> None:
    """Constructor override beats the env-driven default."""
    m = WikiMaintainer(page_store=AsyncMock(), debounce_seconds=42.5)
    assert m._resolve_debounce_seconds() == 42.5


def test_resolve_debounce_seconds_falls_back_to_settings() -> None:
    """When no constructor override is set, the env-driven setting wins."""
    m = WikiMaintainer(page_store=AsyncMock())
    # Settings default is 60 unless overridden in the test environment.
    seconds = m._resolve_debounce_seconds()
    assert isinstance(seconds, float)
    assert seconds >= 0
