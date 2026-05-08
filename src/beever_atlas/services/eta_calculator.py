"""Smoothed ETA via 5-min EWMA of completed-rows-per-second.

Rationale: single-tick rate produces wild ETA swings ("12 days remaining"
→ "5 hours" → "1.2 hours" within minutes — the noise dominates the
signal because individual tick durations vary by 10x under rate-limit
storms). A 5-minute EWMA over ``(timestamp, completed_in_window)``
samples produces a monotone-ish ETA that contracts gradually after
rate-limit recovery instead of overshooting in either direction.

Spec: ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/specs/
sync-progress-feedback/spec.md`` → "Smoothed ETA via 5-minute EWMA".
"""

from __future__ import annotations

from collections.abc import Iterable

WINDOW_SECONDS = 5 * 60
"""5-minute window. Long enough to absorb a single rate-limit burst
without thrashing the ETA, short enough that recovery from a 429
storm produces a visibly contracting ETA within ~5 minutes."""

ALPHA = 0.3
"""EWMA decay factor. ``0.3`` weighs recent ticks meaningfully without
making a single fast tick dominate. Tested empirically — a rate jump
from 5/min to 30/min surfaces in the smoothed value over ~5 minutes."""

MIN_SAMPLES = 3
"""Below this we return None ("calculating"). Two samples can produce a
single rate value, but one rate is not enough to smooth — a rogue first
tick would drive an ETA that the UI then has to retract. Three samples
yield two rate measurements, which is the minimum the EWMA can blend."""


def smoothed_rate(samples: Iterable[tuple[float, int]], now: float) -> float | None:
    """Return rows-per-second EWMA over the last :data:`WINDOW_SECONDS`.

    Parameters
    ----------
    samples:
        Iterable of ``(timestamp_monotonic, rows_completed_in_that_tick)``.
        Zero-row ticks are dropped before windowing — they carry no
        signal about the LLM throughput.
    now:
        Caller's monotonic timestamp used as the window upper-bound.
        Inversion-of-control: tests pass a fixed ``now`` to make EWMA
        deterministic without freezing the clock.

    Returns
    -------
    float | None
        Rows-per-second EWMA, or ``None`` when fewer than
        :data:`MIN_SAMPLES` non-zero samples fall inside the window.
    """
    in_window = [(ts, n) for ts, n in samples if (now - ts) <= WINDOW_SECONDS and n > 0]
    if len(in_window) < MIN_SAMPLES:
        return None
    in_window.sort(key=lambda x: x[0])
    rates: list[float] = []
    for i in range(1, len(in_window)):
        dt = in_window[i][0] - in_window[i - 1][0]
        if dt <= 0:
            continue
        rates.append(in_window[i][1] / dt)
    if not rates:
        return None
    # EWMA, oldest first — the most recent rate carries the largest
    # weight but does not dominate (alpha=0.3).
    ewma = rates[0]
    for r in rates[1:]:
        ewma = ALPHA * r + (1 - ALPHA) * ewma
    return ewma


def smoothed_eta(
    samples: Iterable[tuple[float, int]],
    remaining: int,
    now: float,
) -> int | None:
    """Return seconds-remaining or ``None``.

    ``None`` means "not enough data yet" — the UI should render
    "calculating" instead of a placeholder number that would only
    rapidly retract once real samples accumulate.

    Parameters
    ----------
    samples:
        ``(timestamp_monotonic, rows_completed_in_that_tick)`` pairs.
    remaining:
        Outstanding row count. Zero short-circuits to ``0`` (the run is
        done — no need to wait for sample accumulation).
    now:
        Window upper-bound (monotonic seconds).
    """
    if remaining <= 0:
        return 0
    rate = smoothed_rate(samples, now)
    if rate is None or rate <= 0:
        return None
    return int(remaining / rate)
