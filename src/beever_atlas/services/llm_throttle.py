"""Per-provider token-bucket throttle in front of every LiteLLM call.

Sliding-window accounting on both requests-per-minute (RPM) and
tokens-per-minute (TPM). When either budget would be exceeded the
``acquire`` context manager blocks (``asyncio.sleep``) until the window
slides forward enough to free capacity. Singleton-scoped so every
``litellm.acompletion`` / ``litellm.aembedding`` caller across the
process shares the same view of the configured rates.

Spec: ``openspec/changes/sync-pipeline-feedback-and-auto-wiki/specs/llm-rate-limiting/spec.md``.

Defaults are conservative — sourced from each provider's published
free-tier limits as of 2026-Q1. Operators can override per-provider
via ``LLM_RPM_OVERRIDE_<UPPER_PROVIDER>`` and
``LLM_TPM_OVERRIDE_<UPPER_PROVIDER>`` env vars.

Reactive backoff: when the dispatch layer observes a 429 it calls
``report_429(provider)``. The throttle halves that provider's effective
fill-rate for ``LLM_BACKOFF_COOLDOWN_SECONDS`` (default 60s). Coalesces
overlapping 429s by resetting the cooldown end-time rather than
extending it — multiple bursts collapse into one recovery period.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable

logger = logging.getLogger(__name__)


# Provider RPM/TPM defaults — public free-tier or paid-tier-1 limits as
# documented by each vendor. Conservative on purpose; operators with
# higher quota override per-provider via env. Sources:
#   gemini   — Google AI Studio free tier (10 RPM / 250k TPM, gemini-2.0)
#   openai   — OpenAI tier 1 (500 RPM / 200k TPM for gpt-4o-mini)
#   voyage   — Voyage paid tier (300 RPM / 1M TPM)
#   cohere   — Cohere paid tier (100 RPM / 1M TPM)
#   mistral  — Mistral paid tier (60 RPM / 500k TPM)
#   jina_ai  — Jina paid tier (500 RPM / 1M TPM)
#   ollama   — local; effectively unlimited (10k RPM / 10M TPM cap acts as a
#              safety belt rather than a real throttle).
_DEFAULTS: dict[str, tuple[int, int]] = {
    "gemini": (10, 250_000),
    "openai": (500, 200_000),
    "voyage": (300, 1_000_000),
    "cohere": (100, 1_000_000),
    "mistral": (60, 500_000),
    "jina_ai": (500, 1_000_000),
    "ollama": (10_000, 10_000_000),
}

# Conservative fallback used when a call arrives for an unknown provider.
_FALLBACK_DEFAULT: tuple[int, int] = (60, 1_000_000)

# Window over which RPM/TPM are evaluated. Provider docs publish the
# limits "per minute"; we use a 60-second sliding window so the steady-
# state behaviour matches the published number.
_WINDOW_SECONDS: float = 60.0

# Default cooldown after a 429 is reported. Override via env.
_DEFAULT_COOLDOWN_SECONDS: float = 60.0

# Multiplicative factor applied to the effective rate inside a cooldown.
# 0.5 means "half the configured RPM/TPM". Multiplicative (not additive)
# so future modifiers stack predictably.
_BACKOFF_FACTOR: float = 0.5


class _Bucket:
    """Per-provider sliding-window state.

    ``_events`` records ``(timestamp, est_tokens)`` for every successfully
    acquired call. On each ``acquire`` we drop entries older than the
    window and sum the rest to decide whether to block.
    """

    __slots__ = (
        "provider",
        "rpm_limit",
        "tpm_limit",
        "_events",
        "_lock",
        "_cooldown_until",
        "_logged",
    )

    def __init__(self, provider: str, rpm: int, tpm: int) -> None:
        self.provider = provider
        self.rpm_limit = max(1, int(rpm))
        self.tpm_limit = max(1, int(tpm))
        # Bound the deque so a stuck-forever bucket can't OOM the worker.
        # 10x the RPM limit is generous head-room for the sliding window.
        self._events: deque[tuple[float, int]] = deque(maxlen=max(self.rpm_limit * 10, 200))
        self._lock: asyncio.Lock | None = None
        self._cooldown_until: float = 0.0
        self._logged: bool = False

    def get_lock(self) -> asyncio.Lock:
        # Lazy: creating an asyncio.Lock outside an event loop crashes on
        # 3.10+ when no running loop is available. The throttle module is
        # imported by sync code at startup, so we defer construction.
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def effective_limits(self, now: float) -> tuple[int, int]:
        """Apply the multiplicative backoff factor when inside cooldown."""
        if now < self._cooldown_until:
            return (
                max(1, int(self.rpm_limit * _BACKOFF_FACTOR)),
                max(1, int(self.tpm_limit * _BACKOFF_FACTOR)),
            )
        return (self.rpm_limit, self.tpm_limit)

    def trim(self, now: float) -> None:
        """Drop events outside the sliding window."""
        cutoff = now - _WINDOW_SECONDS
        events = self._events
        while events and events[0][0] < cutoff:
            events.popleft()

    def used(self) -> tuple[int, int]:
        """RPM (count) and TPM (sum) over events currently in the window."""
        rpm = len(self._events)
        tpm = sum(e[1] for e in self._events)
        return rpm, tpm


class LLMThrottle:
    """Singleton-friendly throttle wrapping every LiteLLM call."""

    def __init__(self, *, clock: Callable[[], float] | None = None) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._buckets_lock: asyncio.Lock | None = None
        self._clock: Callable[[], float] = clock or time.monotonic
        # Counters for the metrics endpoint — keep last-60s slice.
        self._blocked_calls: deque[tuple[float, str]] = deque(maxlen=10_000)
        self._recent_429s: deque[tuple[float, str]] = deque(maxlen=1_000)
        self._cooldown_seconds: float = _read_cooldown_seconds()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def acquire(self, provider: str, est_tokens: int) -> AsyncIterator[None]:
        """Block until the bucket has capacity for one call of ``est_tokens``.

        The context manager records the call on entry. The body of the
        ``async with`` is the wrapped LLM call.
        """
        provider_key = (provider or "unknown").strip().lower()
        est_tokens = max(1, int(est_tokens))
        bucket = self._get_or_create_bucket(provider_key)

        # Hot loop: re-evaluate the window on each iteration; sleep the
        # smallest amount that could free capacity so we wake exactly when
        # the oldest in-window event expires.
        blocked_logged = False
        while True:
            async with bucket.get_lock():
                now = self._clock()
                bucket.trim(now)
                rpm_used, tpm_used = bucket.used()
                rpm_limit, tpm_limit = bucket.effective_limits(now)

                rpm_ok = rpm_used + 1 <= rpm_limit
                tpm_ok = tpm_used + est_tokens <= tpm_limit
                if rpm_ok and tpm_ok:
                    bucket._events.append((now, est_tokens))
                    break

                # Compute the soonest moment at which the window will free
                # enough capacity. If RPM blocks, we need the oldest event
                # to age out. If TPM blocks, drop events from the front
                # until cumulative tokens dipped below the budget.
                wait_seconds = _compute_wait(
                    bucket=bucket,
                    now=now,
                    est_tokens=est_tokens,
                    rpm_limit=rpm_limit,
                    tpm_limit=tpm_limit,
                )

            if not blocked_logged:
                self._blocked_calls.append((self._clock(), provider_key))
                blocked_logged = True
                logger.debug(
                    "LLMThrottle: blocking provider=%s rpm_used=%s/%s tpm_used=%s/%s "
                    "est_tokens=%s wait=%.2fs",
                    provider_key,
                    rpm_used,
                    rpm_limit,
                    tpm_used,
                    tpm_limit,
                    est_tokens,
                    wait_seconds,
                )
            await asyncio.sleep(max(wait_seconds, 0.01))

        try:
            yield
        finally:
            # Sliding window: events stay until they age out. No release
            # step is needed; this finally exists for parity with future
            # release semantics (e.g. token-cost reconciliation).
            pass

    def report_429(self, provider: str) -> None:
        """Apply the multiplicative backoff after an observed 429.

        Coalesces overlapping cooldowns: the cooldown end is set to
        ``now + cooldown_seconds`` rather than added to the existing
        end, so a burst of 429s in the same window produces a single
        recovery period that resets each time a new 429 arrives.
        """
        provider_key = (provider or "unknown").strip().lower()
        bucket = self._get_or_create_bucket(provider_key)
        now = self._clock()
        bucket._cooldown_until = now + self._cooldown_seconds
        self._recent_429s.append((now, provider_key))
        logger.warning(
            "LLMThrottle: 429 reported provider=%s cooldown=%.0fs (rate halved)",
            provider_key,
            self._cooldown_seconds,
        )

    def metrics_snapshot(self) -> list[dict[str, object]]:
        """Per-provider live state for the admin metrics endpoint.

        Trims the rolling-window counters to the last 60s on read.
        """
        now = self._clock()
        cutoff = now - _WINDOW_SECONDS
        # Trim the shared counters in place — cheap, bounded by maxlen.
        while self._blocked_calls and self._blocked_calls[0][0] < cutoff:
            self._blocked_calls.popleft()
        while self._recent_429s and self._recent_429s[0][0] < cutoff:
            self._recent_429s.popleft()

        out: list[dict[str, object]] = []
        for provider_key, bucket in self._buckets.items():
            bucket.trim(now)
            rpm_used, tpm_used = bucket.used()
            blocked = sum(1 for _, p in self._blocked_calls if p == provider_key)
            recent_429s = sum(1 for _, p in self._recent_429s if p == provider_key)
            out.append(
                {
                    "provider": provider_key,
                    "rpm_limit": bucket.rpm_limit,
                    "tpm_limit": bucket.tpm_limit,
                    "rpm_used_60s": rpm_used,
                    "tpm_used_60s": tpm_used,
                    "blocked_calls_60s": blocked,
                    "recent_429s_60s": recent_429s,
                    "in_cooldown": now < bucket._cooldown_until,
                }
            )
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _get_or_create_bucket(self, provider_key: str) -> _Bucket:
        bucket = self._buckets.get(provider_key)
        if bucket is not None:
            return bucket
        rpm, tpm = _resolve_limits(provider_key)
        bucket = _Bucket(provider_key, rpm, tpm)
        self._buckets[provider_key] = bucket
        if not bucket._logged:
            if provider_key in _DEFAULTS:
                logger.info(
                    "LLMThrottle: provider=%s rpm=%d tpm=%d (resolved limits)",
                    provider_key,
                    bucket.rpm_limit,
                    bucket.tpm_limit,
                )
            else:
                logger.warning(
                    "LLMThrottle: unknown provider=%s — using fallback rpm=%d tpm=%d",
                    provider_key,
                    bucket.rpm_limit,
                    bucket.tpm_limit,
                )
            bucket._logged = True
        return bucket

    def __repr__(self) -> str:  # pragma: no cover — debug aid
        return f"<LLMThrottle providers={list(self._buckets.keys())}>"


def _resolve_limits(provider_key: str) -> tuple[int, int]:
    """Resolve effective RPM/TPM for a provider, honouring env overrides."""
    upper = provider_key.upper().replace("-", "_")
    rpm_env = os.environ.get(f"LLM_RPM_OVERRIDE_{upper}")
    tpm_env = os.environ.get(f"LLM_TPM_OVERRIDE_{upper}")
    base_rpm, base_tpm = _DEFAULTS.get(provider_key, _FALLBACK_DEFAULT)
    rpm = _coerce_int(rpm_env, base_rpm)
    tpm = _coerce_int(tpm_env, base_tpm)
    return rpm, tpm


def _coerce_int(raw: str | None, fallback: int) -> int:
    if not raw:
        return fallback
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(
            "LLMThrottle: invalid integer override %r — using fallback %d", raw, fallback
        )
        return fallback


def _read_cooldown_seconds() -> float:
    raw = os.environ.get("LLM_BACKOFF_COOLDOWN_SECONDS")
    if not raw:
        return _DEFAULT_COOLDOWN_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        logger.warning(
            "LLMThrottle: invalid LLM_BACKOFF_COOLDOWN_SECONDS=%r — using %.0fs",
            raw,
            _DEFAULT_COOLDOWN_SECONDS,
        )
        return _DEFAULT_COOLDOWN_SECONDS


def _compute_wait(
    *,
    bucket: _Bucket,
    now: float,
    est_tokens: int,
    rpm_limit: int,
    tpm_limit: int,
) -> float:
    """Return the smallest sleep duration that frees enough capacity.

    For RPM: wait until the oldest event ages out so ``rpm_used`` drops
    by 1.

    For TPM: simulate the events expiring in chronological order and
    return the timestamp at which cumulative remaining tokens leaves
    room for ``est_tokens``.
    """
    events = list(bucket._events)
    if not events:
        # No events but we couldn't enter — limit must be ≤ 0 or
        # est_tokens > tpm_limit. Sleep the full window as a guard so we
        # don't busy-loop.
        return _WINDOW_SECONDS

    rpm_used = len(events)
    tpm_used = sum(e[1] for e in events)

    rpm_wait: float = 0.0
    if rpm_used + 1 > rpm_limit:
        # Oldest event ages out at ts + WINDOW. Wait until then.
        rpm_wait = (events[0][0] + _WINDOW_SECONDS) - now

    tpm_wait: float = 0.0
    if tpm_used + est_tokens > tpm_limit:
        # Drop events from the front until the budget fits.
        running = tpm_used
        target_budget = tpm_limit - est_tokens
        for ts, tokens in events:
            running -= tokens
            if running <= target_budget:
                tpm_wait = (ts + _WINDOW_SECONDS) - now
                break
        else:
            # Single call larger than the bucket — wait the full window
            # so we don't tight-loop. Caller should split the request.
            tpm_wait = _WINDOW_SECONDS

    return max(rpm_wait, tpm_wait, 0.0)


# ---------------------------------------------------------------------------
# Singleton accessor
# ---------------------------------------------------------------------------


_singleton: LLMThrottle | None = None


def get_llm_throttle() -> LLMThrottle:
    """Process-wide accessor. Lazy-instantiated on first call."""
    global _singleton
    if _singleton is None:
        _singleton = LLMThrottle()
    return _singleton


def reset_llm_throttle_for_tests() -> None:
    """Test-only helper to drop the singleton between test cases.

    NOT exposed via ``__all__`` — production code should never call this.
    """
    global _singleton
    _singleton = None


__all__ = ["LLMThrottle", "get_llm_throttle"]
