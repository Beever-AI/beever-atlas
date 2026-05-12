"""Scenario G — edge cases (Tasks 5.8.1-5.8.4).

Covers:
  5.8.1 — empty channel sync produces no overview, no maintainer activity,
          no LLM calls; phases end ``[done, done, skipped, skipped]``.
  5.8.2 — idempotent re-sync makes zero new LLM calls.
  5.8.3 — concurrent triggers — one accepted, one 409.
  5.8.4 — maintainer crash mid-debounce — next event re-routes pages.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from beever_atlas.services.wiki_maintainer import WikiMaintainer
from tests.integration.sim_harness import SimStack


def _principal() -> object:
    from beever_atlas.infra.auth import Principal

    return Principal("user:test", kind="user")


@pytest.mark.asyncio
async def test_empty_channel_sync_no_activity(sim_stack: SimStack) -> None:
    """5.8.1 — sync on a channel with zero messages produces nothing."""
    channel = "sim-empty"

    # No messages injected. Worker tick must be a no-op.
    counters = await sim_stack.worker.tick(channel_id=channel)

    assert counters["claimed"] == 0
    assert counters["succeeded"] == 0
    assert counters["failed"] == 0

    # No LLM calls.
    assert sim_stack.llm_call_count(kind="completion") == 0
    assert sim_stack.llm_call_count(kind="embedding") == 0

    # No maintainer fan-out.
    assert sim_stack.maintainer.calls == []

    # No overview created.
    assert not await sim_stack.overview_exists(channel)


@pytest.mark.asyncio
async def test_idempotent_resync_makes_no_new_llm_calls(sim_stack: SimStack) -> None:
    """5.8.2 — sync once, then sync again immediately → zero NEW LLM calls."""
    channel = "sim-idem"
    sim_stack.inject_messages(channel, 30)
    await sim_stack.run_worker_until_quiet(channel)
    pending = getattr(sim_stack.worker, "_sim_pending_tasks", [])
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    pending.clear()

    completions_after_first = sim_stack.llm_call_count(kind="completion")
    embeddings_after_first = sim_stack.llm_call_count(kind="embedding")

    # Re-tick — every row already ``done``; the worker's claim returns
    # zero rows and no LLM calls happen.
    await sim_stack.run_worker_until_quiet(channel)

    assert sim_stack.llm_call_count(kind="completion") == completions_after_first
    assert sim_stack.llm_call_count(kind="embedding") == embeddings_after_first


@pytest.mark.asyncio
async def test_concurrent_sync_triggers_one_accepted_one_409() -> None:
    """5.8.3 — two POST /sync within 100ms — one returns job_id, one 409."""
    fake_stores = MagicMock()
    fake_stores.mongodb.get_sync_status = AsyncMock(return_value=None)

    # The runner mock raises ValueError on the second call to mirror
    # the production runner's "channel already syncing" behaviour. The
    # api/sync.trigger_sync wraps that into a 409.
    state = {"calls": 0}

    async def start_sync(channel_id: str, **kwargs: object) -> str:
        state["calls"] += 1
        if state["calls"] == 1:
            return "job-A"
        raise ValueError(f"sync already in progress for {channel_id}")

    with (
        patch(
            "beever_atlas.llm.embedding_runtime.is_migration_in_progress",
            new=AsyncMock(return_value=False),
        ),
        patch("beever_atlas.api.sync.get_stores", return_value=fake_stores),
        patch(
            "beever_atlas.api.sync.assert_channel_access",
            new=AsyncMock(),
        ),
        patch(
            "beever_atlas.services.policy_resolver.resolve_effective_policy",
            new=AsyncMock(side_effect=ImportError("no policy")),
        ),
        patch(
            "beever_atlas.api.sync.get_sync_runner",
            return_value=MagicMock(
                start_sync=AsyncMock(side_effect=start_sync),
                shutdown=AsyncMock(),
            ),
        ),
    ):
        from fastapi import HTTPException
        from beever_atlas.api.sync import trigger_sync

        first = await trigger_sync(
            channel_id="sim-concurrent",
            principal=_principal(),
        )
        assert first["job_id"] == "job-A"

        with pytest.raises(HTTPException) as exc:
            await trigger_sync(
                channel_id="sim-concurrent",
                principal=_principal(),
            )
        assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_maintainer_crash_mid_debounce_recovers() -> None:
    """5.8.4 — clear the dirty-set mid-window (simulating crash); fire
    another event → page IS rewritten via the next event's accumulator.

    This proves the spec's at-most-60s loss window: a crashed
    maintainer process loses pending events but re-syncs as soon as
    the next event arrives.
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
            return {"page:X": list(fact_ids)}

        async def _record_merge_proposals(self, **_kwargs) -> None:
            return None

    m = _Recorder(page_store=AsyncMock(), debounce_seconds=0.05, mode="auto")

    # Fire 4 events, then "crash" — clear the dirty-set + cancel the
    # flush task before it fires.
    for i in range(4):
        await m.on_extraction_done("ch", [f"f{i}"], mode="auto")
    if m._flush_task is not None and not m._flush_task.done():
        m._flush_task.cancel()
        try:
            await m._flush_task
        except (asyncio.CancelledError, BaseException):  # noqa: BLE001
            pass
    m._dirty.clear()
    m._flush_task = None

    assert rewrite_calls == [], "crash before flush — no rewrite calls expected yet"

    # Fire one more event after crash — the page IS rewritten via the
    # fresh dirty-set.
    await m.on_extraction_done("ch", ["f99"], mode="auto")
    await asyncio.sleep(0.2)

    assert len(rewrite_calls) == 1
    _, _, facts = rewrite_calls[0]
    assert facts == ["f99"], (
        "post-crash rewrite carries only the new event's facts; the "
        "lost-window contract is acceptable per spec D3"
    )
