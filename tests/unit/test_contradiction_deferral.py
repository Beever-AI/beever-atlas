"""Unit tests for P0-1 — deferred ContradictionDetector post-sync pass.

Acceptance criteria covered (per ``.omc/plans/pipeline-cost-latency-reduction-v2.md``):
  (a) concurrent ``memory_settled`` fires don't duplicate work
  (b) epoch default for missing watermark
  (c) watermark advances correctly after a successful run

Plus the kill-switch behaviour around ``defer_contradiction``: when False,
the legacy per-batch path remains the source of truth (covered indirectly
via the config check; the legacy path itself is exercised by
``tests/services/test_contradiction_concurrency.py``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_fact(fact_id: str, entity: str = "Alice"):
    fact = MagicMock()
    fact.id = fact_id
    fact.entity_tags = [entity]
    fact.topic_tags = ["work"]
    fact.memory_text = f"fact {fact_id}"
    fact.invalid_at = None
    return fact


def _make_settings():
    return SimpleNamespace(
        contradiction_concurrency=4,
        contradiction_confidence_threshold=0.8,
        contradiction_flag_threshold=0.5,
        defer_contradiction=True,
    )


def _make_paginated(facts):
    pf = MagicMock()
    pf.memories = facts
    return pf


# ---------------------------------------------------------------------------
# (b) Epoch default for missing watermark — MongoDB store contract
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_contradiction_watermark_epoch_when_row_missing():
    """A channel with no ``channel_sync_state`` row reads as epoch.

    Mirrors the contract documented in ``MongoDBStore.get_contradiction_watermark``:
    fresh channels (no doc) MUST surface ``datetime(1970, 1, 1, tzinfo=UTC)``
    so the very first post-deploy bulk pass covers the entire channel.
    """
    from beever_atlas.stores.mongodb_store import MongoDBStore

    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_sync_state = MagicMock()
    store._channel_sync_state.find_one = AsyncMock(return_value=None)

    wm = await store.get_contradiction_watermark("C1")
    assert wm == datetime(1970, 1, 1, tzinfo=UTC)


@pytest.mark.asyncio
async def test_get_contradiction_watermark_epoch_when_field_absent():
    """Row exists but ``contradiction_watermark`` field is missing.

    Existing pre-deploy rows in ``channel_sync_state`` carry ``last_sync_ts``,
    ``primary_language``, etc. but NOT ``contradiction_watermark``. The
    plan resolves this without migration by treating missing → epoch.
    """
    from beever_atlas.stores.mongodb_store import MongoDBStore

    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_sync_state = MagicMock()
    # Doc exists, but no ``contradiction_watermark`` key.
    store._channel_sync_state.find_one = AsyncMock(
        return_value={"channel_id": "C1", "last_sync_ts": "2025-01-01T00:00:00+00:00"}
    )

    wm = await store.get_contradiction_watermark("C1")
    assert wm == datetime(1970, 1, 1, tzinfo=UTC)


# ---------------------------------------------------------------------------
# (c) Watermark advances correctly after a successful run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_advance_contradiction_watermark_atomic_lte_filter():
    """``advance_contradiction_watermark`` MUST issue a ``$lte`` predicate.

    The architect demand (per plan §1.2 step 6) is that the atomic
    update use ``$lte`` not ``==`` so concurrent invocations cannot
    leapfrog each other. We assert the filter shape directly so a
    refactor cannot silently weaken the guard.
    """
    from beever_atlas.stores.mongodb_store import MongoDBStore

    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_sync_state = MagicMock()
    store._channel_sync_state.find_one_and_update = AsyncMock(return_value={"channel_id": "C1"})

    pre = datetime(2026, 5, 1, tzinfo=UTC)
    post = datetime(2026, 5, 11, tzinfo=UTC)
    advanced = await store.advance_contradiction_watermark(
        channel_id="C1", pre_check=pre, post_check=post
    )

    assert advanced is True
    args, kwargs = store._channel_sync_state.find_one_and_update.call_args
    # First positional arg is the filter dict.
    filt = args[0]
    assert filt["channel_id"] == "C1"
    # Must contain a $lte predicate over contradiction_watermark.
    or_clauses = filt.get("$or") or []
    found_lte = any(
        isinstance(c.get("contradiction_watermark"), dict)
        and "$lte" in c["contradiction_watermark"]
        and c["contradiction_watermark"]["$lte"] == pre
        for c in or_clauses
    )
    assert found_lte, f"Expected $lte clause over contradiction_watermark, got {filt}"
    # Update sets post_check.
    update = args[1]
    assert update["$set"]["contradiction_watermark"] == post


@pytest.mark.asyncio
async def test_advance_contradiction_watermark_returns_false_on_concurrent_advance():
    """When ``find_one_and_update`` returns None, another caller already advanced."""
    from beever_atlas.stores.mongodb_store import MongoDBStore

    store = MongoDBStore.__new__(MongoDBStore)
    store._channel_sync_state = MagicMock()
    store._channel_sync_state.find_one_and_update = AsyncMock(return_value=None)

    pre = datetime(2026, 5, 1, tzinfo=UTC)
    post = datetime(2026, 5, 11, tzinfo=UTC)
    advanced = await store.advance_contradiction_watermark(
        channel_id="C1", pre_check=pre, post_check=post
    )
    assert advanced is False


# ---------------------------------------------------------------------------
# (a) Concurrent memory_settled fires don't duplicate work
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_memory_settled_only_one_advances_watermark():
    """Two concurrent ``check_and_supersede_for_channel`` calls must converge:

    - Both run the per-fact supersession (best-effort, idempotent by design).
    - Only ONE caller observes ``advance_contradiction_watermark → True``.
    - The other observes ``False`` (a sibling already moved the watermark).
    - Neither raises.

    This is the central concurrency guarantee from §1.2 step 6 of the plan.
    """
    from beever_atlas.services import contradiction_detector

    # Simulate the atomic single-winner contract: first caller wins, second
    # observes None. We use a stateful side_effect rather than two AsyncMocks
    # so the helpers are simple.
    advance_call_count = {"n": 0}

    async def _fake_advance(channel_id, pre_check, post_check):
        advance_call_count["n"] += 1
        # First advance succeeds, all subsequent attempts lose the race.
        return advance_call_count["n"] == 1

    pre_wm = datetime(2026, 5, 1, tzinfo=UTC)
    mongodb = MagicMock()
    mongodb.get_contradiction_watermark = AsyncMock(return_value=pre_wm)
    mongodb.advance_contradiction_watermark = AsyncMock(side_effect=_fake_advance)

    facts = [_make_fact(f"f-{i}") for i in range(3)]
    weaviate = MagicMock()
    weaviate.list_facts = AsyncMock(return_value=_make_paginated(facts))

    stores = MagicMock()
    stores.mongodb = mongodb
    stores.weaviate = weaviate

    with (
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch.object(contradiction_detector, "check_and_supersede", new=AsyncMock()),
    ):
        results = await asyncio.gather(
            contradiction_detector.check_and_supersede_for_channel("C1"),
            contradiction_detector.check_and_supersede_for_channel("C1"),
        )

    # Both invocations report the count of new facts they observed.
    assert results == [3, 3]
    # Watermark advancement was attempted exactly twice (once per caller),
    # but only the first attempt won.
    assert advance_call_count["n"] == 2


@pytest.mark.asyncio
async def test_check_and_supersede_for_channel_advances_watermark_on_success():
    """Happy path — facts processed AND watermark advanced once."""
    from beever_atlas.services import contradiction_detector

    pre_wm = datetime(2026, 5, 1, tzinfo=UTC)
    mongodb = MagicMock()
    mongodb.get_contradiction_watermark = AsyncMock(return_value=pre_wm)
    mongodb.advance_contradiction_watermark = AsyncMock(return_value=True)

    facts = [_make_fact("f-1"), _make_fact("f-2")]
    weaviate = MagicMock()
    weaviate.list_facts = AsyncMock(return_value=_make_paginated(facts))
    stores = MagicMock()
    stores.mongodb = mongodb
    stores.weaviate = weaviate

    bulk_calls: list[tuple[str, int]] = []

    async def _fake_bulk(new_facts, channel_id):
        bulk_calls.append((channel_id, len(new_facts)))

    with (
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch.object(
            contradiction_detector, "check_and_supersede", new=AsyncMock(side_effect=_fake_bulk)
        ),
    ):
        count = await contradiction_detector.check_and_supersede_for_channel("C1")

    assert count == 2
    assert bulk_calls == [("C1", 2)]
    mongodb.advance_contradiction_watermark.assert_awaited_once()
    # advance was called with the pre_wm we returned from get_contradiction_watermark.
    call_kwargs = mongodb.advance_contradiction_watermark.await_args.kwargs
    assert call_kwargs["channel_id"] == "C1"
    assert call_kwargs["pre_check"] == pre_wm
    # post_check is wall-clock-derived; assert it is strictly after pre.
    assert call_kwargs["post_check"] > pre_wm


@pytest.mark.asyncio
async def test_check_and_supersede_for_channel_does_not_advance_on_weaviate_failure():
    """Weaviate outage → watermark stays put so the next memory_settled retries."""
    from beever_atlas.services import contradiction_detector

    pre_wm = datetime(2026, 5, 1, tzinfo=UTC)
    mongodb = MagicMock()
    mongodb.get_contradiction_watermark = AsyncMock(return_value=pre_wm)
    mongodb.advance_contradiction_watermark = AsyncMock(return_value=True)

    weaviate = MagicMock()
    weaviate.list_facts = AsyncMock(side_effect=RuntimeError("weaviate down"))
    stores = MagicMock()
    stores.mongodb = mongodb
    stores.weaviate = weaviate

    with (
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch.object(contradiction_detector, "check_and_supersede", new=AsyncMock()),
    ):
        count = await contradiction_detector.check_and_supersede_for_channel("C1")

    assert count == 0
    mongodb.advance_contradiction_watermark.assert_not_awaited()


@pytest.mark.asyncio
async def test_check_and_supersede_for_channel_empty_window_still_advances_watermark():
    """Empty drain → watermark still advances so empty channels don't spin."""
    from beever_atlas.services import contradiction_detector

    pre_wm = datetime(2026, 5, 1, tzinfo=UTC)
    mongodb = MagicMock()
    mongodb.get_contradiction_watermark = AsyncMock(return_value=pre_wm)
    mongodb.advance_contradiction_watermark = AsyncMock(return_value=True)

    weaviate = MagicMock()
    weaviate.list_facts = AsyncMock(return_value=_make_paginated([]))
    stores = MagicMock()
    stores.mongodb = mongodb
    stores.weaviate = weaviate

    with (
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch.object(contradiction_detector, "check_and_supersede", new=AsyncMock()),
    ):
        count = await contradiction_detector.check_and_supersede_for_channel("C1")

    assert count == 0
    mongodb.advance_contradiction_watermark.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_and_supersede_for_channel_explicit_watermark_override():
    """Passing ``watermark_ts`` bypasses the persisted read.

    Used by admin tools that want to re-scan a window without first
    resetting the persisted watermark.
    """
    from beever_atlas.services import contradiction_detector

    mongodb = MagicMock()
    # Persisted watermark would say "2026-05-10" — we override to 1970.
    mongodb.get_contradiction_watermark = AsyncMock(return_value=datetime(2026, 5, 10, tzinfo=UTC))
    mongodb.advance_contradiction_watermark = AsyncMock(return_value=True)

    facts = [_make_fact("f-1")]
    weaviate = MagicMock()
    weaviate.list_facts = AsyncMock(return_value=_make_paginated(facts))
    stores = MagicMock()
    stores.mongodb = mongodb
    stores.weaviate = weaviate

    override = datetime(1970, 1, 1, tzinfo=UTC)

    with (
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch.object(contradiction_detector, "check_and_supersede", new=AsyncMock()),
    ):
        await contradiction_detector.check_and_supersede_for_channel("C1", watermark_ts=override)

    # Persisted watermark was NEVER consulted.
    mongodb.get_contradiction_watermark.assert_not_awaited()
    # Advance used the override as pre_check.
    call_kwargs = mongodb.advance_contradiction_watermark.await_args.kwargs
    assert call_kwargs["pre_check"] == override
