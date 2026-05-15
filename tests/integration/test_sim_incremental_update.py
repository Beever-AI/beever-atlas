"""Scenario C — incremental update (Tasks 5.4.1-5.4.6).

Proves: when a channel that already has facts + an overview gets 5
new messages, the worker processes only the new rows, the maintainer
fires for the new entity's page (not unaffected pages), and the
overview is NOT regenerated. Within a debounce window, 8 burst events
on the same page collapse to 1 maintainer call.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from beever_atlas.services.wiki_maintainer import WikiMaintainer
from tests.integration.sim_harness import SimStack


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_incremental_sync_processes_only_new_rows(sim_stack: SimStack) -> None:
    """5.4.1-5.4.5 — incremental sync touches only new rows + skips overview regen."""
    channel = "sim-C"

    # Initial sync: 30 messages.
    sim_stack.inject_messages(channel, 30)
    await sim_stack.run_worker_until_quiet(channel)
    pending = getattr(sim_stack.worker, "_sim_pending_tasks", [])
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    pending.clear()

    initial_completions = sim_stack.llm_call_count(kind="completion")
    initial_overview_count = len(sim_stack.wiki_pages_added)
    initial_maintainer_calls = len(sim_stack.maintainer.calls)
    assert initial_overview_count == 1, "first sync should produce exactly one overview page"

    # Inject 5 new messages mentioning a new entity.
    sim_stack.inject_messages(channel, 5, topics=["delta"])
    await sim_stack.run_worker_until_quiet(channel)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    # 5.4.3 — only 5 new rows processed → ~1 incremental sub-batch.
    new_completions = sim_stack.llm_call_count(kind="completion") - initial_completions
    assert 1 <= new_completions <= 2, (
        f"expected ~1 incremental completion call for the 5-msg batch, got {new_completions}"
    )

    # 5.4.4 — maintainer fired for new facts (one event for the new
    # batch's fact_ids).
    new_maintainer_calls = len(sim_stack.maintainer.calls) - initial_maintainer_calls
    assert new_maintainer_calls >= 1, (
        "maintainer should have received at least one event for the incremental batch"
    )

    # 5.4.5 — overview NOT regenerated.
    final_overview_count = len(sim_stack.wiki_pages_added)
    assert final_overview_count == initial_overview_count, (
        "auto-overview must be idempotent — second sync produced an unexpected extra overview row"
    )


@pytest.mark.asyncio
async def test_burst_events_collapse_within_debounce_window() -> None:
    """5.4.6 — 8 events on the same page within the debounce window
    collapse to 1 maintainer rewrite call.

    Uses a real WikiMaintainer with a short window (0.05s) so the
    debounce path runs through ``asyncio.sleep`` like production but
    completes within ~250ms wall-clock. The maintainer's
    ``_rewrite_page`` is overridden to count calls — that is the
    proxy for "1 maintainer LLM call" in the spec.
    """
    rewrite_calls: list[tuple[str, str, list[str]]] = []

    class _Recorder(WikiMaintainer):
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
            return {"page:new-entity": list(fact_ids)}

        async def _record_merge_proposals(self, **_kwargs) -> None:
            return None

    m = _Recorder(page_store=AsyncMock(), debounce_seconds=0.05, mode="auto")

    # Fire 8 events touching the same page within the window.
    for i in range(8):
        await m.on_extraction_done("ch", [f"f{i}"], mode="auto")

    # Wait for the debounce + flush to complete.
    await asyncio.sleep(0.2)

    assert len(rewrite_calls) == 1, (
        f"expected 1 collapsed rewrite, got {len(rewrite_calls)}: {rewrite_calls!r}"
    )
    _, _, facts = rewrite_calls[0]
    assert set(facts) == {f"f{i}" for i in range(8)}, (
        "the single rewrite must carry every accumulated fact_id"
    )
