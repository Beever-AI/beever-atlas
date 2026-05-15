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

This throttle wraps both ``litellm.acompletion`` / ``litellm.aembedding``
(via :mod:`llm_dispatch`) and direct Google GenAI SDK calls (via
:meth:`LLMThrottle.throttled_call`). The wiki compiler and maintainer
use ``throttled_call`` so their ``client.aio.models.generate_content``
fan-outs are subject to the same RPM+TPM gate. The legacy ADK ingestion
path in ``batch_processor`` retains its ``aiolimiter`` gate; both layers
coexist and protect different code paths.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")


def _make_bucket_key(provider: str, endpoint_id: str | None) -> str:
    """Compose the throttle bucket key from provider + optional endpoint_id.

    Backward compat: when ``endpoint_id`` is None the key is the provider name
    alone (matches every PR-A dispatch site). When set, the key is
    ``f"{provider}:{endpoint_id}"`` so two same-provider Endpoints get
    independent buckets — see ``agent-llm-provider-pluggable`` design D7.
    """
    base = (provider or "unknown").strip().lower()
    if endpoint_id:
        return f"{base}:{endpoint_id}"
    return base


# Provider RPM/TPM defaults — public free-tier or paid-tier-1 limits as
# documented by each vendor. Conservative on purpose; operators with
# higher quota override per-provider via env. Sources:
#   gemini   — Google AI Studio / paid tier (60 RPM / 500k TPM, gemini-2.0).
#              Raised from the free-tier floor (10 RPM) because wiki builds
#              fan out 15-20 concurrent calls via asyncio.gather and the
#              throttle is now the single RPM gate for both litellm and
#              direct genai SDK paths. Operators on the free tier should set
#              LLM_RPM_OVERRIDE_GEMINI=10 to restore the conservative limit.
#   openai   — OpenAI tier 1 (500 RPM / 200k TPM for gpt-4o-mini)
#   voyage   — Voyage paid tier (300 RPM / 1M TPM)
#   cohere   — Cohere paid tier (100 RPM / 1M TPM)
#   mistral  — Mistral paid tier (60 RPM / 500k TPM)
#   jina_ai  — Jina paid tier (500 RPM / 1M TPM)
#   ollama   — local; effectively unlimited (10k RPM / 10M TPM cap acts as a
#              safety belt rather than a real throttle).
_DEFAULTS: dict[str, tuple[int, int]] = {
    "gemini": (60, 500_000),
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
    async def acquire(
        self,
        provider: str,
        est_tokens: int,
        endpoint_id: str | None = None,
    ) -> AsyncIterator[None]:
        """Block until the bucket has capacity for one call of ``est_tokens``.

        The context manager records the call on entry. The body of the
        ``async with`` is the wrapped LLM call.

        ``endpoint_id`` is optional — when provided, the bucket key becomes
        ``f"{provider}:{endpoint_id}"`` so two same-provider Endpoints get
        independent throttle state (per design D7). When ``None`` the bucket
        is keyed on provider name (backward compat with PR-A call sites).
        """
        provider_key = _make_bucket_key(provider, endpoint_id)
        est_tokens = max(1, int(est_tokens))
        bucket = await self._get_or_create_bucket(provider_key)

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

    def report_429(self, provider: str, endpoint_id: str | None = None) -> None:
        """Apply the multiplicative backoff after an observed 429.

        Coalesces overlapping cooldowns: the cooldown end is set to
        ``now + cooldown_seconds`` rather than added to the existing
        end, so a burst of 429s in the same window produces a single
        recovery period that resets each time a new 429 arrives.

        ``endpoint_id`` is optional — when provided, the cooldown applies to
        the per-Endpoint bucket only (two-orgs scenario).

        Stays synchronous: callers in :mod:`llm_dispatch` already hold
        an :meth:`acquire` context which guarantees the bucket exists;
        the unlocked fallback below is kept only as a defensive guard
        for direct ``report_429`` callers (e.g. tests).
        """
        provider_key = _make_bucket_key(provider, endpoint_id)
        bucket = self._buckets.get(provider_key)
        if bucket is None:
            # Defensive path — should never trigger in production because
            # ``acquire`` runs before ``report_429`` and creates the
            # bucket. Cooldown writes are idempotent (same end-time) so
            # an unlocked create is safe.
            rpm, tpm = _resolve_limits(provider_key)
            bucket = _Bucket(provider_key, rpm, tpm)
            self._buckets[provider_key] = bucket
        now = self._clock()
        bucket._cooldown_until = now + self._cooldown_seconds
        self._recent_429s.append((now, provider_key))
        logger.warning(
            "LLMThrottle: 429 reported provider=%s cooldown=%.0fs (rate halved)",
            provider_key,
            self._cooldown_seconds,
        )

    async def throttled_call(
        self,
        provider: str,
        estimated_tokens: int,
        fn: Callable[..., Awaitable[_T]],
        *args: Any,
        **kwargs: Any,
    ) -> _T:
        """Wrap an arbitrary awaitable LLM call with the per-provider RPM+TPM gate.

        Use this for direct Google GenAI SDK calls (``client.aio.models.generate_content``)
        that bypass the litellm dispatch path. Semantically identical to wrapping
        the call in ``async with self.acquire(provider, estimated_tokens):``.

        If a 429 surfaces (``ResourceExhausted`` or any exception whose message
        contains "429") ``report_429`` is called so the sliding-window backoff
        kicks in immediately.

        Example::

            response = await throttle.throttled_call(
                "gemini",
                estimated_tokens,
                client.aio.models.generate_content,
                model=model_name,
                contents=contents,
                config=config,
            )
        """
        async with self.acquire(provider, estimated_tokens):
            try:
                return await fn(*args, **kwargs)
            except BaseException as exc:
                if _is_429_exc(exc):
                    self.report_429(provider)
                raise

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

    async def _get_or_create_bucket(self, provider_key: str) -> _Bucket:
        """Return the per-provider bucket, creating it on first touch.

        Double-checked locking on ``_buckets_lock`` so two coroutines
        racing on the first call for the same provider converge on a
        single bucket — without the lock, both would build a fresh
        ``_Bucket`` and the loser's writes would clobber the winner's
        sliding-window state, effectively doubling the configured
        rate limit. The fast path (bucket already exists) skips the
        lock entirely.
        """
        bucket = self._buckets.get(provider_key)
        if bucket is not None:
            return bucket
        if self._buckets_lock is None:
            # Lazy: ``asyncio.Lock`` requires a running loop on older
            # Python releases; the throttle module is imported by sync
            # code at startup, so defer construction until first await.
            self._buckets_lock = asyncio.Lock()
        async with self._buckets_lock:
            bucket = self._buckets.get(provider_key)
            if bucket is not None:
                return bucket
            rpm, tpm = _resolve_limits(provider_key)
            bucket = _Bucket(provider_key, rpm, tpm)
            self._buckets[provider_key] = bucket
            # One-shot logging fires inside the lock so it runs exactly
            # once per provider regardless of which coroutine wins the
            # race.
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


def _is_429_exc(exc: BaseException) -> bool:
    """Detect rate-limit errors from the Google GenAI SDK and LiteLLM.

    Google GenAI surfaces 429 as ``google.api_core.exceptions.ResourceExhausted``
    (``status_code`` / ``code`` == 429). LiteLLM wraps them in
    ``litellm.RateLimitError``. We sniff both plus the message text so this
    helper works regardless of which SDK raised.
    """
    try:
        import litellm  # type: ignore[import-untyped]

        if isinstance(exc, litellm.RateLimitError):
            return True
    except Exception:  # noqa: BLE001
        pass
    status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    if status_code == 429:
        return True
    msg = str(exc).lower()
    return "429" in msg or "rate limit" in msg or "rate_limit" in msg or "rate-limit" in msg


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
        # est_tokens > tpm_limit. The oversized-request case would
        # otherwise busy-sleep one window forever (no event ever ages
        # out to free capacity), so raise instead and let the caller
        # split the request. Misconfiguration (limit ≤ 0) still falls
        # through to the safe-guard sleep — operators expect the
        # provider to drain naturally once they fix the override.
        if est_tokens > tpm_limit:
            raise ValueError(
                f"est_tokens={est_tokens} exceeds tpm_limit={tpm_limit} for "
                f"provider={bucket.provider}; caller must split the request"
            )
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
