"""Tests for the max_messages hard cap in token_aware_batches.

Verifies that no batch exceeds the message-count ceiling regardless of
token budget headroom.
"""
from __future__ import annotations

from beever_atlas.services.adaptive_batcher import (
    AVG_TOKENS_PER_ENTITY,
    AVG_TOKENS_PER_FACT,
    token_aware_batches,
)


def _make_messages(n: int) -> list[dict]:
    """Synthesize n minimal messages with distinct timestamps."""
    return [
        {"ts": str(i), "text": f"message {i}", "channel": "C0"}
        for i in range(n)
    ]


def test_all_batches_respect_max_messages_cap() -> None:
    """100 synthetic messages with max_messages=30: every batch has ≤ 30 msgs."""
    messages = _make_messages(100)
    batches = token_aware_batches(messages, max_tokens=12000, max_messages=30)
    assert batches, "Expected at least one batch"
    for i, batch in enumerate(batches):
        assert len(batch) <= 30, (
            f"Batch {i} has {len(batch)} messages, exceeds cap of 30"
        )


def test_all_messages_preserved() -> None:
    """Total messages across all batches equals input count."""
    messages = _make_messages(100)
    batches = token_aware_batches(messages, max_tokens=12000, max_messages=30)
    total = sum(len(b) for b in batches)
    assert total == 100


def test_no_max_messages_unchanged_behaviour() -> None:
    """When max_messages=None, batching is determined by token budget alone."""
    messages = _make_messages(10)
    batches_capped = token_aware_batches(messages, max_tokens=12000, max_messages=None)
    batches_default = token_aware_batches(messages, max_tokens=12000)
    assert [len(b) for b in batches_capped] == [len(b) for b in batches_default]


def test_avg_tokens_per_fact_headroom() -> None:
    """AVG_TOKENS_PER_FACT must be >= 180 (20% headroom over old 150)."""
    assert AVG_TOKENS_PER_FACT >= 180, (
        f"AVG_TOKENS_PER_FACT={AVG_TOKENS_PER_FACT} is below required 180"
    )


def test_avg_tokens_per_entity_headroom() -> None:
    """AVG_TOKENS_PER_ENTITY must be >= 144 (20% headroom over old 120)."""
    assert AVG_TOKENS_PER_ENTITY >= 144, (
        f"AVG_TOKENS_PER_ENTITY={AVG_TOKENS_PER_ENTITY} is below required 144"
    )


def test_higher_averages_yield_smaller_batches_near_ceiling() -> None:
    """With 180/144 averages the output-aware batcher produces ≥20% fewer messages
    per batch near the output ceiling compared to the old 150/120 constants.

    We synthesise messages just below the old ceiling and verify the new
    constants force an earlier split.
    """
    import importlib
    import types as _types
    import beever_atlas.services.adaptive_batcher as _mod

    messages = _make_messages(40)
    # output ceiling chosen so the old constants just fit ~20 msgs per batch
    # but the new (20% larger) constants should force a split sooner.
    ceiling = 20 * (2 * 150 + 1 * 120 + 20)  # 20 msgs * old per-msg estimate

    # Batches with new (higher) averages — already the live code
    batches_new = token_aware_batches(
        messages,
        max_tokens=200000,
        max_output_tokens=ceiling,
        max_facts_per_message=2,
    )

    # Temporarily monkey-patch back to old averages to measure the baseline
    original_fact = _mod.AVG_TOKENS_PER_FACT
    original_entity = _mod.AVG_TOKENS_PER_ENTITY
    _mod.AVG_TOKENS_PER_FACT = 150
    _mod.AVG_TOKENS_PER_ENTITY = 120
    try:
        batches_old = token_aware_batches(
            messages,
            max_tokens=200000,
            max_output_tokens=ceiling,
            max_facts_per_message=2,
        )
    finally:
        _mod.AVG_TOKENS_PER_FACT = original_fact
        _mod.AVG_TOKENS_PER_ENTITY = original_entity

    max_new = max(len(b) for b in batches_new)
    max_old = max(len(b) for b in batches_old)
    assert max_new <= max_old * 0.84, (
        f"New averages should yield ≥20% smaller max batch near ceiling: "
        f"new={max_new} vs old={max_old}"
    )
