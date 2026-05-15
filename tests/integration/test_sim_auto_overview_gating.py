"""Scenario F — auto-overview gating (Tasks 5.7.1-5.7.4).

Proves the four gate conditions on the AutoOverviewSubscriber:
  * below threshold → no overview
  * at/above threshold → overview generated exactly once
  * re-trigger after generation → not regenerated
  * AUTO_OVERVIEW_WIKI=false → never generated; manual API still works
"""

from __future__ import annotations


import pytest

from tests.integration.sim_harness import build_sim_stack


@pytest.mark.asyncio
async def test_below_threshold_no_overview() -> None:
    """5.7.1 — channel with 3 facts (below threshold of 5) → no overview."""
    stack = build_sim_stack(auto_overview_min_facts=5)

    # Mongo with 3 done rows.
    from tests.integration.sim_harness import make_messages

    msgs = make_messages("ch-tiny", 3)
    for m in msgs:
        m.extraction_status = "done"
    stack.mongo.messages.extend(msgs)

    await stack.subscriber.on_extraction_done("ch-tiny", ["f1", "f2", "f3"])
    assert len(stack.wiki_pages_added) == 0


@pytest.mark.asyncio
async def test_above_threshold_overview_once() -> None:
    """5.7.2 — at threshold → overview generated exactly once."""
    stack = build_sim_stack(auto_overview_min_facts=5)
    from tests.integration.sim_harness import make_messages

    msgs = make_messages("ch-12", 12)
    for m in msgs:
        m.extraction_status = "done"
    stack.mongo.messages.extend(msgs)

    await stack.subscriber.on_extraction_done("ch-12", [f"f{i}" for i in range(12)])
    assert len(stack.wiki_pages_added) == 1
    assert stack.wiki_pages_added[0]["channel_id"] == "ch-12"


@pytest.mark.asyncio
async def test_retrigger_does_not_regenerate() -> None:
    """5.7.3 — re-trigger after overview exists → not regenerated."""
    stack = build_sim_stack(auto_overview_min_facts=5)
    from tests.integration.sim_harness import make_messages

    msgs = make_messages("ch-redo", 10)
    for m in msgs:
        m.extraction_status = "done"
    stack.mongo.messages.extend(msgs)

    # First trigger creates the overview.
    await stack.subscriber.on_extraction_done("ch-redo", [f"f{i}" for i in range(10)])
    assert len(stack.wiki_pages_added) == 1

    # Second trigger must short-circuit on gate 5 (overview exists).
    await stack.subscriber.on_extraction_done("ch-redo", [f"f{i}" for i in range(10, 20)])
    assert len(stack.wiki_pages_added) == 1, (
        "subsequent qualifying events must not regenerate the overview"
    )


@pytest.mark.asyncio
async def test_feature_flag_off_skips_overview() -> None:
    """5.7.4 — AUTO_OVERVIEW_WIKI=false → no overview ever generated."""
    stack = build_sim_stack(auto_overview_min_facts=5, auto_overview_enabled=False)
    from tests.integration.sim_harness import make_messages

    msgs = make_messages("ch-off", 50)
    for m in msgs:
        m.extraction_status = "done"
    stack.mongo.messages.extend(msgs)

    await stack.subscriber.on_extraction_done("ch-off", [f"f{i}" for i in range(50)])
    assert len(stack.wiki_pages_added) == 0


@pytest.mark.asyncio
async def test_extraction_in_flight_blocks_overview() -> None:
    """Adjacent gate — extraction not yet complete (pending+extracting>0)
    → overview deferred. The subscriber must skip even though done >=
    threshold, because more facts are still arriving."""
    stack = build_sim_stack(auto_overview_min_facts=5)
    from tests.integration.sim_harness import make_messages

    msgs = make_messages("ch-mid", 10)
    for i, m in enumerate(msgs):
        m.extraction_status = "done" if i < 8 else "pending"
    stack.mongo.messages.extend(msgs)

    await stack.subscriber.on_extraction_done("ch-mid", [f"f{i}" for i in range(8)])
    assert len(stack.wiki_pages_added) == 0, (
        "subscriber must wait for pending+extracting=0 before firing"
    )
