"""Unit tests for token-aware adaptive batching."""

import time

from beever_atlas.services.adaptive_batcher import (
    AVG_TOKENS_PER_FACT,
    estimate_message_output_tokens,
    estimate_message_tokens,
    token_aware_batches,
)


def _msg(text: str = "", ts: str = "1", thread_ts: str | None = None, **extra):
    m = {"text": text, "ts": ts}
    if thread_ts:
        m["thread_ts"] = thread_ts
    m.update(extra)
    return m


class TestEstimateTokens:
    def test_basic_text(self):
        tokens = estimate_message_tokens(_msg("hello world"))  # 11 chars
        assert tokens == 50  # minimum overhead

    def test_long_text(self):
        tokens = estimate_message_tokens(_msg("x" * 900))
        assert tokens == 300  # 900 / 3

    def test_includes_thread_context(self):
        msg = _msg("short", thread_context="x" * 300)
        tokens = estimate_message_tokens(msg)
        assert tokens >= 100  # 300 / 3

    def test_includes_link_metadata(self):
        msg = _msg(
            "hi",
            source_link_titles=["Title One"],
            source_link_descriptions=["Description text here"],
        )
        tokens = estimate_message_tokens(msg)
        assert tokens >= 50

    def test_empty_message(self):
        tokens = estimate_message_tokens({})
        assert tokens == 50  # minimum overhead


class TestTokenAwareBatches:
    def test_empty_input(self):
        assert token_aware_batches([]) == []

    def test_all_fit_one_batch(self):
        msgs = [_msg("hello", ts=str(i)) for i in range(5)]
        batches = token_aware_batches(msgs, max_tokens=5000)
        assert len(batches) == 1
        assert len(batches[0]) == 5

    def test_splits_on_budget(self):
        # Each msg ~333 tokens (1000 chars / 3)
        msgs = [_msg("x" * 1000, ts=str(i)) for i in range(10)]
        batches = token_aware_batches(msgs, max_tokens=1000)
        assert len(batches) > 1
        for batch in batches:
            total = sum(estimate_message_tokens(m) for m in batch)
            # Each batch should be within budget (except oversized single groups)
            assert total <= 1000 or len(batch) == 1

    def test_single_message_exceeds_budget(self):
        msgs = [_msg("x" * 60000, ts="1")]  # ~20000 tokens
        batches = token_aware_batches(msgs, max_tokens=1000)
        assert len(batches) == 1
        assert len(batches[0]) == 1

    def test_thread_group_preservation(self):
        parent = _msg("parent message", ts="100")
        reply1 = _msg("reply one", ts="101", thread_ts="100")
        reply2 = _msg("reply two", ts="102", thread_ts="100")
        other = _msg("other message", ts="200")

        batches = token_aware_batches(
            [parent, reply1, reply2, other],
            max_tokens=200,  # small budget forces split
        )
        # Parent and replies must be in the same batch
        for batch in batches:
            ts_set = {m["ts"] for m in batch}
            if "100" in ts_set:
                assert "101" in ts_set and "102" in ts_set

    def test_chronological_order(self):
        msgs = [_msg("c", ts="3"), _msg("a", ts="1"), _msg("b", ts="2")]
        batches = token_aware_batches(msgs, max_tokens=50000)
        assert len(batches) == 1
        timestamps = [m["ts"] for m in batches[0]]
        assert timestamps == ["1", "2", "3"]

    def test_many_small_messages(self):
        msgs = [_msg("hi", ts=str(i)) for i in range(100)]
        batches = token_aware_batches(msgs, max_tokens=2000)
        total_msgs = sum(len(b) for b in batches)
        assert total_msgs == 100  # no messages lost


# -----------------------------------------------------------------------------
# Output-aware batching (new in eliminate-llm-eof-errors change)
# -----------------------------------------------------------------------------


class TestEstimateOutputTokens:
    def test_scales_with_max_facts(self):
        m = _msg("any")
        low = estimate_message_output_tokens(m, max_facts_per_message=1)
        high = estimate_message_output_tokens(m, max_facts_per_message=3)
        assert high > low
        assert high - low == 2 * AVG_TOKENS_PER_FACT


class TestOutputBudget:
    def test_default_preserves_behavior(self):
        """Without max_output_tokens, behavior is byte-identical to pre-change."""
        msgs = [_msg(f"m{i}", ts=str(i)) for i in range(20)]
        baseline = token_aware_batches(msgs, max_tokens=5000)
        new = token_aware_batches(msgs, max_tokens=5000, max_output_tokens=None)
        assert [len(b) for b in baseline] == [len(b) for b in new]

    def test_output_budget_forces_more_batches(self):
        """Tight output budget must split a fact-dense input into more batches."""
        msgs = [_msg(f"m{i}", ts=str(i)) for i in range(30)]
        loose = token_aware_batches(msgs, max_tokens=100_000)
        tight = token_aware_batches(
            msgs, max_tokens=100_000, max_output_tokens=500, max_facts_per_message=2
        )
        assert len(tight) > len(loose)
        total = sum(len(b) for b in tight)
        assert total == 30  # no loss

    def test_thread_group_never_split(self):
        """Parent + replies must land in the same batch even under tight budget."""
        msgs = [
            _msg("parent", ts="100"),
            _msg("reply1", ts="101", thread_ts="100"),
            _msg("reply2", ts="102", thread_ts="100"),
            _msg("other", ts="200"),
        ]
        batches = token_aware_batches(
            msgs,
            max_tokens=100_000,
            max_output_tokens=200,  # extremely tight
        )
        # The thread group (ts 100/101/102) must remain together.
        for b in batches:
            ids = [m["ts"] for m in b]
            has_parent = "100" in ids
            has_any_reply = "101" in ids or "102" in ids
            if has_parent or has_any_reply:
                assert "100" in ids and "101" in ids and "102" in ids, (
                    f"thread group split across batches: {ids}"
                )

    def test_no_messages_lost_under_any_budget(self):
        msgs = [_msg(f"m{i}", ts=str(i)) for i in range(50)]
        for out_budget in (None, 200, 1000, 5000):
            batches = token_aware_batches(msgs, max_tokens=3000, max_output_tokens=out_budget)
            assert sum(len(b) for b in batches) == 50


# -----------------------------------------------------------------------------
# Dry-run performance mock — ensures adaptive batcher stays fast on realistic
# inputs after the two-sided-budget change.
# -----------------------------------------------------------------------------


class TestBatcherPerformance:
    def _make_messages(self, n: int) -> list:
        # Mix: 70% standalone, 30% thread replies.
        msgs = []
        for i in range(n):
            if i % 10 >= 7 and i > 0:
                msgs.append(_msg(f"reply {i}", ts=str(i), thread_ts=str(i - 1)))
            else:
                msgs.append(_msg(f"msg {i} " + ("x" * 400), ts=str(i)))
        return msgs

    def test_perf_default_1000_messages(self):
        msgs = self._make_messages(1000)
        t0 = time.perf_counter()
        batches = token_aware_batches(msgs, max_tokens=12_000)
        elapsed = time.perf_counter() - t0
        assert sum(len(b) for b in batches) == 1000
        # Issue #55 — sanity ceiling, not a perf gate. The batcher is pure
        # Python with no I/O so 1k messages typically completes in <50ms;
        # 3.0s gives ~60x headroom for loaded CI hosts while still catching
        # O(n²) regressions (those would push past several seconds at 1k).
        # Algorithmic regression detection lives in
        # test_perf_parity_default_vs_output_aware (relative, load-immune).
        assert elapsed < 3.0, f"default batching too slow: {elapsed:.3f}s"

    def test_perf_with_output_budget_1000_messages(self):
        msgs = self._make_messages(1000)
        t0 = time.perf_counter()
        batches = token_aware_batches(
            msgs, max_tokens=12_000, max_output_tokens=90_000, max_facts_per_message=2
        )
        elapsed = time.perf_counter() - t0
        assert sum(len(b) for b in batches) == 1000
        # Issue #55 — see test_perf_default_1000_messages for threshold
        # rationale; output-aware regression vs. default is checked
        # relatively in test_perf_parity_default_vs_output_aware.
        assert elapsed < 3.0, f"output-aware batching too slow: {elapsed:.3f}s"

    def test_perf_parity_default_vs_output_aware(self):
        """Output-aware path must not be more than 3x slower than default."""
        msgs = self._make_messages(500)
        t0 = time.perf_counter()
        for _ in range(5):
            token_aware_batches(msgs, max_tokens=12_000)
        default_t = time.perf_counter() - t0

        t0 = time.perf_counter()
        for _ in range(5):
            token_aware_batches(msgs, max_tokens=12_000, max_output_tokens=90_000)
        output_t = time.perf_counter() - t0

        # Allow generous slack; output path adds one extra sum per group.
        assert output_t < default_t * 3.0 + 0.1, (
            f"output-aware regressed: default={default_t:.3f}s output={output_t:.3f}s"
        )
