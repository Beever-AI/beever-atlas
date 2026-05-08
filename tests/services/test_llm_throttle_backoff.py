"""Reactive-backoff tests for LLMThrottle (B2 task 2.2.9).

Uses an injected fake clock so we can advance "time" without actually
sleeping for 60 seconds. The throttle module reads ``time.monotonic``
by default but accepts a ``clock=`` kwarg for testability.
"""

from __future__ import annotations

import os

import pytest

from beever_atlas.services.llm_throttle import LLMThrottle


class FakeClock:
    """Manually-advanced monotonic clock for cooldown-window tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("LLM_RPM_OVERRIDE_") or key.startswith("LLM_TPM_OVERRIDE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("LLM_BACKOFF_COOLDOWN_SECONDS", raising=False)


def test_429_halves_effective_rate() -> None:
    """report_429 halves the bucket's effective limits for the cooldown."""
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "10"
    os.environ["LLM_TPM_OVERRIDE_GEMINI"] = "1000"
    try:
        clock = FakeClock(start=1000.0)
        t = LLMThrottle(clock=clock)

        # report_429's defensive path materializes the bucket synchronously
        # when it does not yet exist — ``_get_or_create_bucket`` is async
        # now (race-safe) so the sync test path goes through report_429.
        t.report_429("gemini")
        bucket = t._buckets["gemini"]

        # Inside the cooldown window: limits halved.
        rpm, tpm = bucket.effective_limits(clock.now)
        assert rpm == 5
        assert tpm == 500

        # Mid-cooldown: still halved.
        clock.advance(30.0)
        rpm, tpm = bucket.effective_limits(clock.now)
        assert rpm == 5
        assert tpm == 500

        # After the cooldown ends: configured rate restored.
        clock.advance(31.0)  # 30 + 31 = 61s past the report
        rpm, tpm = bucket.effective_limits(clock.now)
        assert rpm == 10
        assert tpm == 1000
    finally:
        os.environ.pop("LLM_RPM_OVERRIDE_GEMINI", None)
        os.environ.pop("LLM_TPM_OVERRIDE_GEMINI", None)


def test_overlapping_429s_do_not_stack() -> None:
    """A second 429 inside the window resets, not extends, the cooldown."""
    os.environ["LLM_RPM_OVERRIDE_GEMINI"] = "10"
    try:
        clock = FakeClock(start=1000.0)
        t = LLMThrottle(clock=clock)

        t.report_429("gemini")
        bucket = t._buckets["gemini"]
        first_end = bucket._cooldown_until
        assert first_end == pytest.approx(1060.0)

        # 30 seconds later, a second 429 arrives.
        clock.advance(30.0)
        t.report_429("gemini")
        second_end = bucket._cooldown_until

        # New end is now+60, NOT first_end+60.
        assert second_end == pytest.approx(1090.0)
        # Specifically less than the stacked alternative (1120s).
        assert second_end < first_end + 60.0
    finally:
        os.environ.pop("LLM_RPM_OVERRIDE_GEMINI", None)


def test_cooldown_seconds_env_override() -> None:
    """LLM_BACKOFF_COOLDOWN_SECONDS overrides the default cooldown."""
    os.environ["LLM_BACKOFF_COOLDOWN_SECONDS"] = "10"
    try:
        clock = FakeClock(start=500.0)
        t = LLMThrottle(clock=clock)
        t.report_429("gemini")
        bucket = t._buckets["gemini"]
        assert bucket._cooldown_until == pytest.approx(510.0)
    finally:
        os.environ.pop("LLM_BACKOFF_COOLDOWN_SECONDS", None)


def test_recent_429s_metric_trims_after_window() -> None:
    """recent_429s_60s only includes the last 60s of reports."""
    clock = FakeClock(start=1000.0)
    t = LLMThrottle(clock=clock)
    t.report_429("gemini")
    t.report_429("gemini")
    snap = t.metrics_snapshot()
    entry = next(p for p in snap if p["provider"] == "gemini")
    assert entry["recent_429s_60s"] == 2

    # Jump way past the window — 429s age out of the metric.
    clock.advance(120.0)
    snap = t.metrics_snapshot()
    entry = next(p for p in snap if p["provider"] == "gemini")
    assert entry["recent_429s_60s"] == 0
