"""Principal-keyed sliding-window rate limiter for the MCP surface.

Architecture note (v1 limitation):
    This implementation uses a single in-process dict keyed on
    ``(principal_id, tool_name)``. It is **not** distributed-cache-aware:
    when multiple worker processes serve the ``/mcp`` mount, each process
    maintains independent counters, so a principal can call each worker up to
    N times per window (not N times globally). For v1 single-process deployments
    this is acceptable.

Upgrade path:
    Replace ``_WINDOWS`` with a Redis-backed sliding-window counter using the
    same ``check_and_record`` signature. The rest of the codebase only imports
    this function so the swap is zero-touch for callers.
"""

from __future__ import annotations

import time
from collections import defaultdict
from typing import Callable

# ---------------------------------------------------------------------------
# Per-tool limits (calls per 60-second window)
# ---------------------------------------------------------------------------

_DEFAULT_LIMIT = 60
_TOOL_LIMITS: dict[str, int] = {
    "ask_channel": 30,
    "trigger_sync": 5,
    "refresh_wiki": 5,
}

# ---------------------------------------------------------------------------
# In-memory sliding-window state
# Key: (principal_id, tool_name) -> list of call timestamps (monotonic)
# ---------------------------------------------------------------------------

_windows: dict[tuple[str, str], list[float]] = defaultdict(list)

# Injected clock — override in tests via ``_set_clock(callable)``
_clock: Callable[[], float] = time.monotonic

_WINDOW_SECONDS = 60


def _set_clock(fn: Callable[[], float]) -> None:
    """Replace the wall-clock; for unit tests only."""
    global _clock
    _clock = fn


def reset_state() -> None:
    """Clear all rate-limit counters; for unit tests only.

    Tests MUST call this in their setup/teardown so state does not leak
    across test cases.
    """
    _windows.clear()


async def check_and_record(
    principal_id: str,
    tool_name: str,
) -> tuple[bool, int | None]:
    """Check whether ``principal_id`` may invoke ``tool_name`` right now.

    Updates the sliding window unconditionally (so each call is counted
    even if denied — this prevents callers from hammering the limiter at
    zero cost when already throttled).

    Returns:
        ``(True, None)``  — call is allowed.
        ``(False, int)``  — call is denied; ``int`` is the number of seconds
                            until the oldest recorded call falls out of the
                            window (i.e. the earliest the caller may retry).
    """
    limit = _TOOL_LIMITS.get(tool_name, _DEFAULT_LIMIT)
    now = _clock()
    window_start = now - _WINDOW_SECONDS

    key = (principal_id, tool_name)
    # Prune stale entries
    timestamps = [t for t in _windows[key] if t >= window_start]

    if len(timestamps) >= limit:
        # Oldest timestamp in the window — retry after it falls off.
        oldest = timestamps[0]
        retry_after = int(oldest + _WINDOW_SECONDS - now) + 1
        # Record the rejected call so the window stays accurate.
        timestamps.append(now)
        _windows[key] = timestamps
        return False, max(retry_after, 1)

    timestamps.append(now)
    _windows[key] = timestamps
    return True, None


__all__ = [
    "check_and_record",
    "reset_state",
    "_set_clock",
]
