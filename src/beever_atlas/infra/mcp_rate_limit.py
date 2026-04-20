"""Principal-keyed sliding-window rate limiter for the MCP surface.

Two backends are supported, selected by ``settings.beever_mcp_rate_limit_backend``:

- ``"memory"`` — per-process in-memory sliding window. Default; sufficient for
  single-worker deploys. In multi-worker deploys each process counts
  independently, so a principal can burst up to N times per worker.

- ``"redis"`` — distributed sliding window backed by a Redis sorted set. Call
  timestamps are stored under ``mcp:rl:{principal}:{tool}`` with the timestamp
  as both score and member. Each ``check_and_record`` invocation runs an
  atomic Lua script that:

    1. Drops entries older than ``now - window`` (sliding expiry).
    2. Counts remaining entries.
    3. If the count is below the limit, records ``now`` and returns allowed.
    4. Otherwise returns the seconds-until-retry (i.e. when the oldest entry
       will expire).

  The Lua script guarantees atomicity across concurrent worker processes — no
  race between check and record. The key TTL is refreshed to ``window`` on
  every write so idle keys self-clean.

Public API (``check_and_record``, ``reset_state``, ``_set_clock``) is identical
across backends so callers do not care which is active.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import Callable

from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Per-tool limits (calls per 60-second window)
# ---------------------------------------------------------------------------

_DEFAULT_LIMIT = 60
_TOOL_LIMITS: dict[str, int] = {
    "ask_channel": 30,
    "trigger_sync": 5,
    "refresh_wiki": 5,
}

_WINDOW_SECONDS = 60


# ---------------------------------------------------------------------------
# Clock injection for deterministic tests
# ---------------------------------------------------------------------------

_clock: Callable[[], float] = time.monotonic


def _set_clock(fn: Callable[[], float]) -> None:
    """Replace the wall-clock; for unit tests only."""
    global _clock
    _clock = fn


# ---------------------------------------------------------------------------
# Memory backend
# ---------------------------------------------------------------------------

# Key: (principal_id, tool_name) -> list of call timestamps (monotonic)
_windows: dict[tuple[str, str], list[float]] = defaultdict(list)


async def _check_and_record_memory(principal_id: str, tool_name: str) -> tuple[bool, int | None]:
    limit = _TOOL_LIMITS.get(tool_name, _DEFAULT_LIMIT)
    now = _clock()
    window_start = now - _WINDOW_SECONDS

    key = (principal_id, tool_name)
    timestamps = [t for t in _windows[key] if t >= window_start]

    if len(timestamps) >= limit:
        oldest = timestamps[0]
        retry_after = int(oldest + _WINDOW_SECONDS - now) + 1
        # Fix #9: store the pruned list but DO NOT append the rejected
        # timestamp. Otherwise an abusive caller can grow the list without
        # bound by firing faster than the window can expire entries.
        _windows[key] = timestamps
        return False, max(retry_after, 1)

    timestamps.append(now)
    _windows[key] = timestamps
    return True, None


# ---------------------------------------------------------------------------
# Redis backend
# ---------------------------------------------------------------------------

# Atomic sliding-window Lua script. KEYS[1] is the sorted-set key.
# ARGV: [now, window_seconds, limit, member_suffix]
# Returns: {allowed (0|1), retry_after_seconds (int)} — retry_after is 0 when allowed.
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local member_suffix = ARGV[4]
local window_start = now - window

-- Drop entries older than the window.
redis.call('ZREMRANGEBYSCORE', key, '-inf', window_start)

local count = tonumber(redis.call('ZCARD', key))

if count >= limit then
    -- Use oldest surviving score to compute retry_after.
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local retry_after = 1
    if oldest and oldest[2] then
        retry_after = math.max(1, math.ceil(tonumber(oldest[2]) + window - now))
    end
    return {0, retry_after}
end

-- Record this call. Use now + suffix so concurrent calls in the same
-- millisecond still get distinct members.
redis.call('ZADD', key, now, tostring(now) .. ':' .. member_suffix)
redis.call('EXPIRE', key, window)
return {1, 0}
"""

_redis_client = None  # type: ignore[var-annotated]
_redis_script_sha: str | None = None


async def _get_redis():
    """Return a shared asyncio Redis client, lazily initialised."""
    global _redis_client, _redis_script_sha

    if _redis_client is not None:
        return _redis_client

    import redis.asyncio as aioredis

    settings = get_settings()
    _redis_client = aioredis.from_url(settings.redis_url, decode_responses=True)
    try:
        _redis_script_sha = await _redis_client.script_load(_SLIDING_WINDOW_LUA)
    except Exception:
        # We still work without cached sha — eval_sha fallback handled below.
        _redis_script_sha = None
    return _redis_client


async def _check_and_record_redis(principal_id: str, tool_name: str) -> tuple[bool, int | None]:
    import uuid

    global _redis_script_sha

    client = await _get_redis()
    limit = _TOOL_LIMITS.get(tool_name, _DEFAULT_LIMIT)
    now = _clock()
    key = f"mcp:rl:{principal_id}:{tool_name}"
    # Uuid suffix makes ZADD members unique when two calls land in the same
    # microsecond (score collisions would otherwise silently drop one).
    suffix = uuid.uuid4().hex[:8]

    try:
        if _redis_script_sha is not None:
            try:
                result = await client.evalsha(
                    _redis_script_sha,
                    1,
                    key,
                    str(now),
                    str(_WINDOW_SECONDS),
                    str(limit),
                    suffix,
                )
            except Exception:
                # Script evicted from Redis cache — re-load and fall through.
                _redis_script_sha = await client.script_load(_SLIDING_WINDOW_LUA)
                result = await client.evalsha(
                    _redis_script_sha,
                    1,
                    key,
                    str(now),
                    str(_WINDOW_SECONDS),
                    str(limit),
                    suffix,
                )
        else:
            result = await client.eval(
                _SLIDING_WINDOW_LUA,
                1,
                key,
                str(now),
                str(_WINDOW_SECONDS),
                str(limit),
                suffix,
            )
    except Exception:
        # Redis unreachable: fall back to the in-process memory limiter so
        # we degrade into a per-worker slider rather than disabling rate
        # limiting entirely. A pure fail-open would let an attacker burn
        # expensive tools (ask_channel, trigger_sync) without bound whenever
        # Redis is flaky. The warning remains so ops notice the spike.
        logger.warning(
            "event=mcp_rate_limit_redis_unavailable "
            "principal=%s tool=%s — falling back to in-process memory limiter",
            principal_id,
            tool_name,
            exc_info=True,
        )
        return await _check_and_record_memory(principal_id, tool_name)

    allowed = int(result[0]) == 1
    retry_after = int(result[1]) if not allowed else None
    return allowed, retry_after


# ---------------------------------------------------------------------------
# Public API (backend dispatch)
# ---------------------------------------------------------------------------


async def check_and_record(principal_id: str, tool_name: str) -> tuple[bool, int | None]:
    """Check whether ``principal_id`` may invoke ``tool_name`` right now.

    Returns ``(True, None)`` if allowed or ``(False, retry_after_seconds)``
    if denied. Denied calls are NOT recorded in the memory backend (Fix #9
    — keeping rejected timestamps would let an abusive caller grow the
    window list without bound); the Redis backend's Lua script is already
    atomic and also only records on accept.
    """
    settings = get_settings()
    backend = getattr(settings, "beever_mcp_rate_limit_backend", "memory")
    if backend == "redis":
        return await _check_and_record_redis(principal_id, tool_name)
    return await _check_and_record_memory(principal_id, tool_name)


def reset_state() -> None:
    """Clear in-process rate-limit counters; for unit tests only.

    Always synchronous — callers (existing and new) don't need to await.
    The Redis backend does NOT share state across tests by design: each
    Redis test sets its own principal/tool pair so counter leakage is
    bounded. If a Redis test truly needs a wipe, call
    :func:`reset_state_redis` explicitly.
    """
    _windows.clear()


async def reset_state_redis() -> None:
    """Delete every ``mcp:rl:*`` key in Redis. Redis-backend tests only."""
    try:
        client = await _get_redis()
        async for key in client.scan_iter(match="mcp:rl:*", count=500):
            await client.delete(key)
    except Exception:
        logger.warning(
            "event=mcp_rate_limit_reset_redis_failed — state may leak across tests",
            exc_info=True,
        )


__all__ = [
    "check_and_record",
    "reset_state",
    "reset_state_redis",
    "_set_clock",
]
