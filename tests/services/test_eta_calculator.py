"""Unit tests for ``services/eta_calculator.py`` (Phase 3 / Task 4.1.4).

The smoothed ETA calculator must:
  - return ``None`` until at least :data:`MIN_SAMPLES` samples are in
    the window (so the UI can render "calculating" instead of a wild
    placeholder),
  - short-circuit to ``0`` when ``remaining=0``,
  - produce a stable ETA when the tick rate is steady,
  - contract gradually (not instantly) when the rate jumps after a
    rate-limit recovery.

Spec: ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/specs/
sync-progress-feedback/spec.md`` → "Smoothed ETA via 5-minute EWMA".
"""

from __future__ import annotations

from beever_atlas.services.eta_calculator import smoothed_eta


def test_returns_none_when_below_min_samples() -> None:
    """Fewer than ``MIN_SAMPLES`` samples yields None — UI shows
    "calculating"."""
    assert smoothed_eta([(0.0, 5)], remaining=100, now=10.0) is None
    assert smoothed_eta([(0.0, 5), (1.0, 5)], remaining=100, now=10.0) is None


def test_returns_zero_when_remaining_is_zero() -> None:
    """Run is done — short-circuit before the rate computation."""
    assert smoothed_eta([(0.0, 5), (1.0, 5), (2.0, 5)], remaining=0, now=2.0) == 0


def test_stable_rate_produces_stable_eta() -> None:
    """30 rows/min steady rate → ETAs should not vary by >2x."""
    # 5 rows every 10s = 0.5 rows/sec
    samples = [(float(t), 5) for t in range(0, 60, 10)]
    eta1 = smoothed_eta(samples, remaining=300, now=60.0)
    samples_later = samples + [(70.0, 5), (80.0, 5)]
    eta2 = smoothed_eta(samples_later, remaining=290, now=80.0)
    assert eta1 is not None and eta2 is not None
    assert 0.5 * eta1 < eta2 < 2.0 * eta1


def test_rate_change_eta_contracts_gradually() -> None:
    """A rate jump should NOT instantly snap the ETA to the new value.

    Spec scenario: rate jumps from ~5/min to ~30/min (6x). The EWMA
    must dampen the change so the smoothed ETA contracts visibly but
    not by the full ratio after just a couple of samples.
    """
    # Steady slow rate of ~5 rows per minute (1 row every 12 seconds)
    # for 5 minutes.
    slow = [(float(t), 1) for t in range(0, 300, 12)]
    # Then a faster rate: 6 rows every 12 seconds (~30 rows/min) for the
    # next 60 seconds. Same cadence as the slow samples — this isolates
    # the rate jump from a sample-density change.
    fast = [(300.0 + i * 12.0, 6) for i in range(1, 6)]

    eta_slow_only = smoothed_eta(slow, remaining=600, now=300.0)
    # Two fast samples in: EWMA still heavily influenced by the slow
    # history.
    eta_two_fast = smoothed_eta(slow + fast[:2], remaining=600, now=324.0)

    assert eta_slow_only is not None and eta_two_fast is not None
    # The smoothed ETA must contract (jump is real) but not collapse to
    # the raw 1/6 ratio after only two new samples — the EWMA blends.
    ratio = eta_two_fast / eta_slow_only
    assert 0.2 < ratio < 0.95, f"ratio={ratio:.3f} should be a gradual contraction"


def test_zero_rate_returns_none() -> None:
    """All-zero samples → no successful claims in the window → None."""
    samples = [(0.0, 0), (1.0, 0), (2.0, 0)]
    assert smoothed_eta(samples, remaining=100, now=2.0) is None
