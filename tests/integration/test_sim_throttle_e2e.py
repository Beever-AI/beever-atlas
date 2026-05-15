"""Scenario E — throttle prevents 429 storms (Tasks 5.6.1-5.6.5).

Drives 25 concurrent ``dispatch_completion`` calls under a 5-RPM
gemini bucket. The throttle MUST gate them so no call sees a 429 and
the wall-clock spread is consistent with the rate. To keep the test
fast we compress the throttle window to 1s (matching the existing
``test_llm_throttle_integration`` pattern) — same gating logic,
shorter test window.

Variant: simulate one 429 → next-minute rate halves → recovery after
the cooldown expires.
"""

from __future__ import annotations

import asyncio
import os
import time
from collections.abc import Iterator
from typing import Any

import pytest

from beever_atlas.services import llm_throttle as throttle_mod
from beever_atlas.services.llm_throttle import reset_llm_throttle_for_tests


@pytest.fixture(autouse=True)
def _reset_throttle(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_llm_throttle_for_tests()
    for key in list(os.environ):
        if key.startswith("LLM_RPM_OVERRIDE_") or key.startswith("LLM_TPM_OVERRIDE_"):
            monkeypatch.delenv(key, raising=False)
    yield
    reset_llm_throttle_for_tests()


@pytest.mark.asyncio
async def test_throttle_gates_25_calls_to_5_rpm(monkeypatch: pytest.MonkeyPatch) -> None:
    """5.6.1-5.6.4 — 25 concurrent dispatch_completions under 5-RPM
    take ~5 windows wall-clock; no 429 leaks through."""
    monkeypatch.setattr(throttle_mod, "_WINDOW_SECONDS", 1.0)
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "5"
    os.environ["LLM_TPM_OVERRIDE_GEMINI"] = "10000000"

    fake_completions: list[dict[str, Any]] = []

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        fake_completions.append({"model": model, "ts": time.monotonic()})

        class _Resp:
            status_code = 200

        return _Resp()

    import litellm  # type: ignore[import-untyped]

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from beever_atlas.services.llm_dispatch import dispatch_completion

    started = time.monotonic()

    async def one(idx: int) -> None:
        await dispatch_completion(
            provider="gemini",
            model="gemini/gemini-2.5-flash",
            messages=[{"role": "user", "content": f"hello {idx}"}],
        )

    await asyncio.gather(*(one(i) for i in range(25)))
    elapsed = time.monotonic() - started

    assert len(fake_completions) == 25
    # 5 RPM × 5 windows of 1s = ~5s. Allow 4-9s for scheduler jitter.
    assert elapsed >= 4.0, f"throttle did not gate (too fast): {elapsed:.2f}s"
    assert elapsed <= 9.0, f"throttle gated too long: {elapsed:.2f}s"

    # 5.6.3 — no 429 reported because throttle blocked them.
    from beever_atlas.services.llm_throttle import get_llm_throttle

    snap = get_llm_throttle().metrics_snapshot()
    gemini = next(p for p in snap if p["provider"] == "gemini")
    assert gemini["recent_429s_60s"] == 0


@pytest.mark.asyncio
async def test_throttle_halves_rate_after_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """5.6.5 — variant: provider returns one 429; the throttle's
    reactive backoff halves the bucket's effective fill-rate for the
    cooldown window. After the cooldown expires the original rate
    resumes.

    The cooldown is 60s in production. We compress the throttle's
    sliding window to 1s here so the test exercises the gating logic
    shape without waiting a real minute.
    """
    monkeypatch.setattr(throttle_mod, "_WINDOW_SECONDS", 1.0)
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "10"

    import litellm  # type: ignore[import-untyped]
    from tests.integration.sim_harness import _StubRateLimit

    monkeypatch.setattr(litellm, "RateLimitError", _StubRateLimit, raising=False)

    calls: list[float] = []
    raise_429_until: dict[str, float] = {"until": time.monotonic() + 0.05}

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> Any:
        calls.append(time.monotonic())
        if time.monotonic() < raise_429_until["until"]:
            raise _StubRateLimit("first call 429")

        class _Resp:
            status_code = 200

        return _Resp()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from beever_atlas.services.llm_dispatch import dispatch_completion
    from beever_atlas.services.llm_throttle import get_llm_throttle

    # First call observes the 429 and the throttle records it.
    with pytest.raises(_StubRateLimit):
        await dispatch_completion(
            provider="gemini",
            model="gemini/gemini-2.5-flash",
            messages=[{"role": "user", "content": "x"}],
        )

    snap = get_llm_throttle().metrics_snapshot()
    gemini = next(p for p in snap if p["provider"] == "gemini")
    assert gemini["recent_429s_60s"] == 1
    assert gemini["in_cooldown"] is True

    # The bucket's effective limits are halved during cooldown — the
    # rate-limit instance API exposes this via the metrics shape.
    bucket = get_llm_throttle()._buckets["gemini"]
    rpm_eff, _tpm_eff = bucket.effective_limits(time.monotonic())
    assert rpm_eff == 5, "after 429 the effective RPM should halve from 10 → 5"
