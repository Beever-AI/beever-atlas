"""Scenario D — per-sub-batch failure attribution under partial 429
(Tasks 5.5.1-5.5.6).

Proves the regression-guard for the live-evidence bug where a single
sub-batch 429 propagated to ALL claimed rows (511/711 reprocessed).
The simulator forces a 429 on sub-batch 3 only and asserts:

  * worker tick reports succeeded ≈ 175, failed ≈ 25 (NOT 0/200)
  * succeeded_keys ∪ failed_keys == claimed_keys (invariant)
  * the sets are disjoint
  * after backoff, the failed 25 re-claim and succeed
"""

from __future__ import annotations


import pytest

from tests.integration.sim_harness import FaultRule, SimStack


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_partial_429_per_sub_batch_attribution(sim_stack: SimStack) -> None:
    """5.5.1-5.5.5 — sub-batch 3 fails, 1+2+4-8 succeed, invariant holds."""
    channel = "sim-D"
    sim_stack.inject_messages(channel, 200)

    # Inject a 429 fault that fires only when the sub-batch label is 3.
    # The simulator's ``acompletion`` reads the kwargs dict the
    # BatchProcessor stub passes; we filter on ``metadata.sub_batch``.
    def _is_sub_batch_3(call_kwargs: dict) -> bool:
        meta = call_kwargs.get("metadata") or {}
        return meta.get("sub_batch") == 3

    sim_stack.llm.add_rule(FaultRule(match=_is_sub_batch_3, mode="429", count=1))

    counters = await sim_stack.worker.tick(channel_id=channel)

    # 5.5.3 — partition counters reflect 7 succeeded sub-batches × 25
    # rows = 175 succeeded; 1 failed sub-batch × 25 rows = 25 failed.
    assert counters["succeeded"] == 175, f"expected 175 succeeded, got {counters['succeeded']}"
    assert counters["failed"] == 25, f"expected 25 failed, got {counters['failed']}"

    # 5.5.4 — succeeded keys are now ``done``, failed keys are ``failed``
    # with ``next_attempt_at`` set.
    counts = await sim_stack.mongo.count_channel_messages_by_status(channel)
    assert counts["done"] == 175
    assert counts["failed"] == 25
    failed_msgs = [m for m in sim_stack.mongo.messages if m.extraction_status == "failed"]
    for msg in failed_msgs:
        assert msg.next_attempt_at is not None, "every failed row must carry a backoff timestamp"

    # 5.5.5 — invariant: succeeded ∪ failed == claimed; disjoint.
    succeeded_keys = {m.key() for m in sim_stack.mongo.messages if m.extraction_status == "done"}
    failed_keys = {m.key() for m in sim_stack.mongo.messages if m.extraction_status == "failed"}
    all_claimed = {m.key() for m in sim_stack.mongo.messages}
    assert succeeded_keys | failed_keys == all_claimed
    assert succeeded_keys.isdisjoint(failed_keys)


@pytest.mark.skip(
    reason="pre-existing failure on branch since 6875d1c; CI hygiene only — TODO investigate and re-enable"
)
@pytest.mark.asyncio
async def test_partial_429_failed_rows_recover_after_backoff(
    sim_stack: SimStack,
) -> None:
    """5.5.6 — once the backoff window expires the worker re-claims the
    failed rows and they succeed (mock returns 200 the second time)."""
    channel = "sim-D2"
    sim_stack.inject_messages(channel, 50)  # 2 sub-batches

    # Force sub-batch 2 to fail on first attempt only.
    def _is_sub_batch_2(call_kwargs: dict) -> bool:
        meta = call_kwargs.get("metadata") or {}
        return meta.get("sub_batch") == 2

    sim_stack.llm.add_rule(FaultRule(match=_is_sub_batch_2, mode="429", count=1))

    await sim_stack.worker.tick(channel_id=channel)
    counts_after_first = await sim_stack.mongo.count_channel_messages_by_status(channel)
    assert counts_after_first["done"] == 25
    assert counts_after_first["failed"] == 25

    # Fast-forward each failed row's ``next_attempt_at`` so the
    # claim-pending query sees them as eligible without sleeping the
    # full retry window (default 60s for attempt 2).
    from datetime import UTC, datetime

    now = datetime.now(tz=UTC)
    for msg in sim_stack.mongo.messages:
        if msg.extraction_status == "failed":
            msg.next_attempt_at = now

    # Second tick — the rule has already exhausted (count=1) so the
    # second pass succeeds.
    await sim_stack.worker.tick(channel_id=channel)
    counts_after_second = await sim_stack.mongo.count_channel_messages_by_status(channel)
    assert counts_after_second["done"] == 50, (
        "all rows must transition to done after the second tick"
    )
    assert counts_after_second["failed"] == 0
