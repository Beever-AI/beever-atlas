"""Unit tests for LLMThrottle bucket behaviour (B2 task 2.2.8).

Tests use a 1-second sliding window (monkeypatched ``_WINDOW_SECONDS``)
so the suite runs in seconds instead of minutes while still exercising
the real sleep-and-retry path. The acceptance scenarios in
``specs/llm-rate-limiting/spec.md`` are framed in 60s windows; the
math is identical at any window length, so a compressed window is a
faithful test.
"""

from __future__ import annotations

import asyncio
import os
import time

import pytest

from beever_atlas.services import llm_throttle as throttle_mod
from beever_atlas.services.llm_throttle import LLMThrottle


@pytest.fixture(autouse=True)
def _reset_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    # Clear any operator-supplied overrides so the test sees deterministic
    # defaults — _DEFAULTS unless we override them per-test.
    for key in list(os.environ):
        if key.startswith("LLM_RPM_OVERRIDE_") or key.startswith("LLM_TPM_OVERRIDE_"):
            monkeypatch.delenv(key, raising=False)


@pytest.fixture
def short_window(monkeypatch: pytest.MonkeyPatch) -> float:
    """Compress the sliding window to 1s so tests run fast."""
    window = 1.0
    monkeypatch.setattr(throttle_mod, "_WINDOW_SECONDS", window)
    return window


@pytest.mark.asyncio
async def test_rpm_gate_blocks_burst(short_window: float) -> None:
    """20 calls to a 10-RPM bucket take ~one window-length wall-clock.

    With a 1-second window: the first 10 acquires fire immediately, then
    the next 10 each block until the oldest event ages out. Total wall
    clock should be ~1 second (one window), within ±50% slack for the
    asyncio scheduler.
    """
    monkeypatch_env = {"LLM_RPM_OVERRIDE_GEMINI": "10", "LLM_TPM_OVERRIDE_GEMINI": "10000000"}
    for k, v in monkeypatch_env.items():
        os.environ[k] = v
    try:
        t = LLMThrottle()
        started = time.monotonic()

        async def call() -> None:
            async with t.acquire("gemini", est_tokens=1):
                pass

        await asyncio.gather(*(call() for _ in range(20)))
        elapsed = time.monotonic() - started
        # First batch of 10 fires instantly; second batch waits ~one
        # window. Allow generous margin for slow CI machines.
        assert elapsed >= short_window * 0.8, f"too fast: {elapsed:.2f}s"
        assert elapsed <= short_window * 3.0, f"too slow: {elapsed:.2f}s"
    finally:
        for k in monkeypatch_env:
            os.environ.pop(k, None)


@pytest.mark.asyncio
async def test_tpm_is_binding_constraint(short_window: float) -> None:
    """5 calls × 60_000 tokens against a 250k-TPM bucket gates the 5th."""
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "1000"  # don't gate on RPM
    os.environ["LLM_TPM_OVERRIDE_GEMINI"] = "250000"
    try:
        t = LLMThrottle()
        started = time.monotonic()

        async def call() -> None:
            async with t.acquire("gemini", est_tokens=60_000):
                pass

        await asyncio.gather(*(call() for _ in range(5)))
        elapsed = time.monotonic() - started
        # 4 fit (4×60k = 240k), 5th must wait one window for the oldest
        # event to age out.
        assert elapsed >= short_window * 0.8, f"5th call did not gate: {elapsed:.2f}s"
    finally:
        os.environ.pop("LLM_RPM_OVERRIDE_GEMINI", None)
        os.environ.pop("LLM_TPM_OVERRIDE_GEMINI", None)


@pytest.mark.asyncio
async def test_two_providers_do_not_interfere(short_window: float) -> None:
    """Exhausting gemini does not block openai."""
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "2"
    os.environ["LLM_TPM_OVERRIDE_GEMINI"] = "1000"
    os.environ["LLM_RPM_OVERRIDE_OPENAI"] = "1000"
    os.environ["LLM_TPM_OVERRIDE_OPENAI"] = "1000000"
    try:
        t = LLMThrottle()

        # Exhaust gemini bucket.
        async with t.acquire("gemini", est_tokens=10):
            pass
        async with t.acquire("gemini", est_tokens=10):
            pass

        # openai call should fire immediately even though gemini is full.
        started = time.monotonic()
        async with t.acquire("openai", est_tokens=10):
            pass
        elapsed = time.monotonic() - started
        assert elapsed < 0.1, f"openai blocked by gemini state: {elapsed:.3f}s"
    finally:
        os.environ.pop("LLM_RPM_OVERRIDE_GEMINI", None)
        os.environ.pop("LLM_TPM_OVERRIDE_GEMINI", None)
        os.environ.pop("LLM_RPM_OVERRIDE_OPENAI", None)
        os.environ.pop("LLM_TPM_OVERRIDE_OPENAI", None)


@pytest.mark.asyncio
async def test_unknown_provider_falls_back(short_window: float) -> None:
    """Unknown provider gets fallback limits.

    Coalesced single-warning behaviour is verified by ``_logged`` flag
    on the bucket — a direct attribute check avoids the project's JSON
    log formatter which bypasses pytest's ``caplog`` propagation.
    """
    t = LLMThrottle()
    async with t.acquire("acme_unicorn_llm", est_tokens=1):
        pass
    bucket = t._buckets["acme_unicorn_llm"]
    # First-use logging happened — flag is set so subsequent uses no-op.
    assert bucket._logged is True
    # Second use should leave the flag set (single-warning invariant).
    async with t.acquire("acme_unicorn_llm", est_tokens=1):
        pass
    assert bucket._logged is True
    snap = t.metrics_snapshot()
    entry = next(p for p in snap if p["provider"] == "acme_unicorn_llm")
    assert entry["rpm_limit"] == 60  # fallback default
    assert entry["tpm_limit"] == 1_000_000


@pytest.mark.asyncio
async def test_metrics_snapshot_shape(short_window: float) -> None:
    """Metrics endpoint returns the documented per-provider shape."""
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "5"
    os.environ["LLM_TPM_OVERRIDE_GEMINI"] = "10000"
    try:
        t = LLMThrottle()
        async with t.acquire("gemini", est_tokens=100):
            pass
        snap = t.metrics_snapshot()
        assert len(snap) == 1
        e = snap[0]
        assert e["provider"] == "gemini"
        assert e["rpm_limit"] == 5
        assert e["tpm_limit"] == 10000
        assert e["rpm_used_60s"] == 1
        assert e["tpm_used_60s"] == 100
        assert e["blocked_calls_60s"] == 0
        assert e["recent_429s_60s"] == 0
        assert e["in_cooldown"] is False
    finally:
        os.environ.pop("LLM_RPM_OVERRIDE_GEMINI", None)
        os.environ.pop("LLM_TPM_OVERRIDE_GEMINI", None)


@pytest.mark.asyncio
async def test_concurrent_first_touch_creates_single_bucket(
    short_window: float,
) -> None:
    """Two coroutines acquiring the same fresh provider concurrently
    must converge on a single shared bucket — no clobber, no 2x limit.

    Without the double-checked lock in ``_get_or_create_bucket``, both
    racers would build a fresh ``_Bucket`` and the loser's writes would
    overwrite the winner's sliding-window state, effectively doubling
    the configured rate limit on the cold start.
    """
    throttle = LLMThrottle()

    async def _acq() -> None:
        async with throttle.acquire(provider="brand_new_provider", est_tokens=1):
            pass

    await asyncio.gather(_acq(), _acq(), _acq(), _acq())

    # Exactly one bucket exists for the provider.
    assert "brand_new_provider" in throttle._buckets
    bucket = throttle._buckets["brand_new_provider"]
    # All four events landed in the same bucket — no clobber.
    assert len(bucket._events) == 4


@pytest.mark.asyncio
async def test_oversized_request_raises_rather_than_hangs(
    short_window: float,
) -> None:
    """A single est_tokens that exceeds TPM must raise — not block forever.

    Without the guard in ``_compute_wait``, the worker would sleep the
    full window indefinitely (no event ever ages out to free capacity)
    and the caller could never make progress.
    """
    throttle = LLMThrottle()
    # Force a tiny TPM via override so the est_tokens easily exceeds it.
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "100"
    os.environ["LLM_TPM_OVERRIDE_GEMINI"] = "1000"
    try:
        with pytest.raises(ValueError, match="exceeds tpm_limit"):
            async with throttle.acquire(provider="gemini", est_tokens=5000):
                pass
    finally:
        os.environ.pop("LLM_RPM_OVERRIDE_GEMINI", None)
        os.environ.pop("LLM_TPM_OVERRIDE_GEMINI", None)
