"""Tests for parallel ContradictionDetector per-fact loop (P4-A).

Verifies that check_and_supersede runs fact checks concurrently bounded by
settings.contradiction_concurrency, with per-fact error isolation.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fact(fact_id: str, entity: str = "Alice") -> MagicMock:
    fact = MagicMock()
    fact.id = fact_id
    fact.entity_tags = [entity]
    fact.topic_tags = ["work"]
    fact.memory_text = f"fact {fact_id}"
    fact.invalid_at = None
    return fact


def _make_existing_fact(fact_id: str) -> MagicMock:
    ef = MagicMock()
    ef.id = fact_id
    ef.entity_tags = ["Alice"]
    ef.topic_tags = ["work"]
    ef.memory_text = f"existing {fact_id}"
    ef.invalid_at = None
    return ef


def _make_settings(concurrency: int = 4):
    return SimpleNamespace(
        contradiction_concurrency=concurrency,
        contradiction_confidence_threshold=0.8,
        contradiction_flag_threshold=0.5,
    )


def _make_stores(existing_facts=None, *, supersede_side_effect=None):
    weaviate = AsyncMock()
    result = MagicMock()
    result.memories = existing_facts or []
    weaviate.list_facts = AsyncMock(return_value=result)
    weaviate.supersede_fact = AsyncMock(side_effect=supersede_side_effect)
    weaviate.flag_potential_contradiction = AsyncMock()
    stores = MagicMock()
    stores.weaviate = weaviate
    return stores


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_facts_checked_in_parallel():
    """All new facts are checked; concurrent execution bounded by semaphore."""
    from beever_atlas.services.contradiction_detector import check_and_supersede

    new_facts = [_make_fact(f"new-{i}") for i in range(6)]
    existing = [_make_existing_fact("old-1")]
    stores = _make_stores(existing)
    settings = _make_settings(concurrency=4)

    call_order: list[str] = []
    original_detect = None

    async def _fake_detect(new_fact, candidates):
        call_order.append(new_fact.id)
        return []

    with (
        patch("beever_atlas.services.contradiction_detector.get_settings", return_value=settings),
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch("beever_atlas.services.contradiction_detector.detect_contradictions", side_effect=_fake_detect),
    ):
        await check_and_supersede(new_facts, channel_id="C1")

    assert len(call_order) == 6, f"Expected 6 detect calls, got {len(call_order)}"


@pytest.mark.asyncio
async def test_per_fact_error_isolation():
    """One fact failing detection does not prevent other facts from being checked."""
    from beever_atlas.services.contradiction_detector import check_and_supersede

    new_facts = [_make_fact(f"new-{i}") for i in range(4)]
    existing = [_make_existing_fact("old-1")]
    stores = _make_stores(existing)
    settings = _make_settings(concurrency=4)

    checked: list[str] = []

    async def _fake_detect(new_fact, candidates):
        if new_fact.id == "new-1":
            raise RuntimeError("simulated LLM failure")
        checked.append(new_fact.id)
        return []

    with (
        patch("beever_atlas.services.contradiction_detector.get_settings", return_value=settings),
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch("beever_atlas.services.contradiction_detector.detect_contradictions", side_effect=_fake_detect),
    ):
        # Should not raise
        await check_and_supersede(new_facts, channel_id="C1")

    # 3 facts succeed; new-1 failed but others continued
    assert "new-0" in checked
    assert "new-2" in checked
    assert "new-3" in checked


@pytest.mark.asyncio
async def test_semaphore_bounds_concurrency():
    """No more than contradiction_concurrency facts run simultaneously."""
    from beever_atlas.services.contradiction_detector import check_and_supersede

    concurrency = 2
    new_facts = [_make_fact(f"new-{i}") for i in range(6)]
    existing = [_make_existing_fact("old-1")]
    stores = _make_stores(existing)
    settings = _make_settings(concurrency=concurrency)

    in_flight = 0
    max_in_flight = 0

    async def _fake_detect(new_fact, candidates):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)  # yield to let others run
        in_flight -= 1
        return []

    with (
        patch("beever_atlas.services.contradiction_detector.get_settings", return_value=settings),
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch("beever_atlas.services.contradiction_detector.detect_contradictions", side_effect=_fake_detect),
    ):
        await check_and_supersede(new_facts, channel_id="C1")

    assert max_in_flight <= concurrency, (
        f"Expected max concurrency {concurrency}, observed {max_in_flight}"
    )


@pytest.mark.asyncio
async def test_supersede_called_on_high_confidence():
    """Facts with confidence >= threshold trigger supersede_fact."""
    from beever_atlas.services.contradiction_detector import check_and_supersede

    new_fact = _make_fact("new-1")
    existing = [_make_existing_fact("old-1")]
    stores = _make_stores(existing)
    settings = _make_settings()

    async def _fake_detect(nf, candidates):
        return [{"existing_fact_id": "old-1", "confidence": 0.9, "reason": "conflict"}]

    with (
        patch("beever_atlas.services.contradiction_detector.get_settings", return_value=settings),
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch("beever_atlas.services.contradiction_detector.detect_contradictions", side_effect=_fake_detect),
    ):
        await check_and_supersede([new_fact], channel_id="C1")

    stores.weaviate.supersede_fact.assert_awaited_once_with(
        old_fact_id="old-1", new_fact_id="new-1"
    )


@pytest.mark.asyncio
async def test_flag_called_on_mid_confidence():
    """Facts with confidence in [flag_threshold, confidence_threshold) trigger flag."""
    from beever_atlas.services.contradiction_detector import check_and_supersede

    new_fact = _make_fact("new-1")
    existing = [_make_existing_fact("old-1")]
    stores = _make_stores(existing)
    settings = _make_settings()

    async def _fake_detect(nf, candidates):
        return [{"existing_fact_id": "old-1", "confidence": 0.6, "reason": "maybe"}]

    with (
        patch("beever_atlas.services.contradiction_detector.get_settings", return_value=settings),
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch("beever_atlas.services.contradiction_detector.detect_contradictions", side_effect=_fake_detect),
    ):
        await check_and_supersede([new_fact], channel_id="C1")

    stores.weaviate.flag_potential_contradiction.assert_awaited_once_with("new-1")
    stores.weaviate.supersede_fact.assert_not_awaited()


@pytest.mark.asyncio
async def test_facts_without_tags_skipped():
    """Facts with no entity or topic tags are skipped without calling detect_contradictions."""
    from beever_atlas.services.contradiction_detector import check_and_supersede

    no_tags_fact = _make_fact("new-1")
    no_tags_fact.entity_tags = []
    no_tags_fact.topic_tags = []

    stores = _make_stores()
    settings = _make_settings()
    detect_calls: list[str] = []

    async def _fake_detect(nf, candidates):
        detect_calls.append(nf.id)
        return []

    with (
        patch("beever_atlas.services.contradiction_detector.get_settings", return_value=settings),
        patch("beever_atlas.stores.get_stores", return_value=stores),
        patch("beever_atlas.services.contradiction_detector.detect_contradictions", side_effect=_fake_detect),
    ):
        await check_and_supersede([no_tags_fact], channel_id="C1")

    assert detect_calls == [], "detect_contradictions should not be called for tagless facts"
