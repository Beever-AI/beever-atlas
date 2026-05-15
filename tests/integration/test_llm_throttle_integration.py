"""Integration test for the throttle + dispatch_completion path (B2 task 2.2.10).

Verifies that ``dispatch_completion`` actually gates the call rate via
the throttle. To keep the suite fast we compress the throttle's
sliding window down to 1s and configure a 5-RPM bucket; the spec
asks for "5 minutes nominal" with a 5-RPM/60s window — same shape,
just compressed.
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
def _reset(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    reset_llm_throttle_for_tests()
    for key in list(os.environ):
        if key.startswith("LLM_RPM_OVERRIDE_") or key.startswith("LLM_TPM_OVERRIDE_"):
            monkeypatch.delenv(key, raising=False)
    yield
    reset_llm_throttle_for_tests()


@pytest.mark.asyncio
async def test_dispatch_completion_enforces_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """25 mock dispatch_completion calls under 5-RPM gemini take ~5 windows.

    The spec scenario uses a 60s window (5 RPM × 5 windows = 5 minutes).
    We compress to 1s windows here (5 RPM × 5 windows = 5 seconds) so
    the test runs quickly while exercising the same gating logic.
    """
    monkeypatch.setattr(throttle_mod, "_WINDOW_SECONDS", 1.0)
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "5"
    os.environ["LLM_TPM_OVERRIDE_GEMINI"] = "10000000"  # don't gate on TPM

    fake_completions: list[dict[str, Any]] = []

    class _Resp:
        status_code = 200

        def __init__(self, idx: int) -> None:
            self.idx = idx

    async def fake_acompletion(*, model: str, messages: Any, **kwargs: Any) -> _Resp:
        fake_completions.append({"model": model})
        return _Resp(len(fake_completions))

    # Patch litellm.acompletion only (the dispatch wrapper imports
    # litellm lazily, so we patch on the module attribute that the
    # wrapper resolves at call time).
    import litellm

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    from beever_atlas.services.llm_dispatch import dispatch_completion

    started = time.monotonic()

    async def one_call(idx: int) -> None:
        await dispatch_completion(
            provider="gemini",
            model="gemini/gemini-2.5-flash",
            messages=[{"role": "user", "content": f"hello {idx}"}],
        )

    await asyncio.gather(*(one_call(i) for i in range(25)))
    elapsed = time.monotonic() - started

    # Expectation: 5 RPM × 5 windows = 5 windows of 1s = ~5s.
    # Allow ±50% slack for scheduler jitter on slow CI hardware.
    assert len(fake_completions) == 25
    assert elapsed >= 4.0, f"too fast — throttle did not gate: {elapsed:.2f}s"
    assert elapsed <= 8.0, f"too slow: {elapsed:.2f}s"

    # No 429s emitted by the mock, so the throttle should report none.
    from beever_atlas.services.llm_throttle import get_llm_throttle

    snap = get_llm_throttle().metrics_snapshot()
    entry = next(p for p in snap if p["provider"] == "gemini")
    assert entry["recent_429s_60s"] == 0


@pytest.mark.asyncio
async def test_dispatch_completion_reports_429(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raised RateLimitError feeds back into report_429 on the throttle."""
    monkeypatch.setattr(throttle_mod, "_WINDOW_SECONDS", 1.0)
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "100"  # don't gate on RPM

    import litellm

    class _StubRateLimit(Exception):
        status_code = 429

    # LiteLLM's RateLimitError is what the dispatcher checks first;
    # patch it to a class that's an Exception subclass so the test
    # can both raise and detect it.
    monkeypatch.setattr(litellm, "RateLimitError", _StubRateLimit, raising=False)

    async def boom(*args: Any, **kwargs: Any) -> Any:
        raise _StubRateLimit("429 from mock")

    monkeypatch.setattr(litellm, "acompletion", boom)

    from beever_atlas.services.llm_dispatch import dispatch_completion
    from beever_atlas.services.llm_throttle import get_llm_throttle

    with pytest.raises(_StubRateLimit):
        await dispatch_completion(
            provider="gemini",
            model="gemini/gemini-2.5-flash",
            messages=[{"role": "user", "content": "x"}],
        )

    snap = get_llm_throttle().metrics_snapshot()
    entry = next(p for p in snap if p["provider"] == "gemini")
    assert entry["recent_429s_60s"] == 1
    assert entry["in_cooldown"] is True
