"""Per-tool MCP observability — lightweight structured-log emitter.

Design (v1 — best-effort structured logging):
    This module emits structured log lines via the existing
    :mod:`beever_atlas.infra.logging` ``StructuredFormatter``.  It is NOT a
    proper OpenTelemetry pipeline: no spans, no metrics SDK, no export daemon.
    Emission is best-effort — if the logger is unavailable or the log handler
    raises, the exception is swallowed so tool handling is never blocked.

Upgrade path (v2):
    1. Add ``opentelemetry-sdk`` and a metrics exporter (OTLP / Prometheus).
    2. Replace ``record_tool_call`` with an OTel ``Counter`` + ``Histogram``.
    3. Keep the same function signature so callers need zero changes.
    4. Remove the ``best_effort`` wrapper once the OTel pipeline is reliable.
"""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory rolling counter for the operator view (task 7.6)
# ---------------------------------------------------------------------------
#
# Per-process ring buffer of recent tool calls. Not a proper TSDB — we keep
# the last ``_COUNTER_WINDOW_SECONDS`` of (ts, principal, tool, outcome,
# duration_ms) tuples in memory so `/api/dev/mcp-metrics` can summarise them
# without reading logs. In multi-worker deploys each worker reports its own
# slice; the dashboard aggregates client-side. Production-grade metrics
# should upgrade to OTel (see top-of-file upgrade path).

_COUNTER_WINDOW_SECONDS = 3600  # 1 hour
_MAX_BUFFER_SIZE = 10_000  # cap at 10k events per process (cheap memory bound)
_event_buffer: list[tuple[float, str, str, str, float]] = []
_buffer_lock = Lock()


def _prune_buffer_locked(now: float) -> None:
    """Drop events older than the window. Caller must hold ``_buffer_lock``."""
    global _event_buffer
    cutoff = now - _COUNTER_WINDOW_SECONDS
    # Binary-search-equivalent since timestamps are monotonically appended.
    i = 0
    for i, (ts, *_) in enumerate(_event_buffer):
        if ts >= cutoff:
            break
    else:
        if _event_buffer and _event_buffer[-1][0] < cutoff:
            _event_buffer = []
            return
    _event_buffer = _event_buffer[i:]


def snapshot_counters(now: float | None = None) -> dict[str, Any]:
    """Return an aggregated snapshot of the rolling metrics window.

    Shape: ``{"window_seconds", "total_calls", "by_principal_tool": [...],
              "by_outcome": {...}, "principals": int}``.
    Principal ids are returned verbatim (they are already non-reversible
    ``mcp:<hash>`` tokens — safe to surface to operators with the admin
    token).
    """
    now = now or time.time()
    with _buffer_lock:
        _prune_buffer_locked(now)
        events = list(_event_buffer)

    by_key: dict[tuple[str, str, str], int] = defaultdict(int)
    by_outcome: dict[str, int] = defaultdict(int)
    durations_by_tool: dict[str, list[float]] = defaultdict(list)
    principals: set[str] = set()

    for _ts, principal, tool, outcome, duration_ms in events:
        by_key[(principal, tool, outcome)] += 1
        by_outcome[outcome] += 1
        durations_by_tool[tool].append(duration_ms)
        principals.add(principal)

    by_principal_tool = [
        {
            "principal": principal,
            "tool": tool,
            "outcome": outcome,
            "count": count,
        }
        for (principal, tool, outcome), count in sorted(by_key.items(), key=lambda x: -x[1])
    ]

    by_tool_latency = {
        tool: {
            "count": len(durations),
            "avg_ms": round(sum(durations) / len(durations), 2),
            "p95_ms": round(sorted(durations)[int(0.95 * (len(durations) - 1))], 2)
            if durations
            else 0.0,
        }
        for tool, durations in durations_by_tool.items()
    }

    return {
        "window_seconds": _COUNTER_WINDOW_SECONDS,
        "total_calls": len(events),
        "distinct_principals": len(principals),
        "by_outcome": dict(by_outcome),
        "by_principal_tool": by_principal_tool,
        "by_tool_latency": by_tool_latency,
    }


def reset_counters() -> None:
    """Clear the in-memory event buffer. For tests / ops reset only."""
    with _buffer_lock:
        _event_buffer.clear()


def record_tool_call(
    tool_name: str,
    principal_hash: str,
    outcome: str,
    duration_ms: float,
    target: str | None = None,
    **extra: Any,
) -> None:
    """Emit one structured ``mcp_tool_call_metric`` log line.

    Args:
        tool_name:      MCP tool name (e.g. ``"ask_channel"``).
        principal_hash: The full principal id (``mcp:<hash16>``).  Already
                        a non-reversible hash — safe to log verbatim.
        outcome:        One of ``"ok"`` or an error code from the mcp-auth
                        error catalog (``"channel_access_denied"``,
                        ``"rate_limited"``, ``"connection_access_denied"``,
                        ``"job_not_found"``, ``"answer_timeout"``,
                        ``"authentication_missing"``, ``"exception"``, …).
        duration_ms:    Wall-clock elapsed time in milliseconds.
        target:         Optional resource identifier affected by the call
                        (channel_id, connection_id, job_id, …).
        **extra:        Additional key-value pairs merged into the log record.

    Emission is best-effort — any exception is logged at DEBUG level and
    swallowed so the tool handler is never interrupted by an observability error.
    """
    try:
        data: dict[str, Any] = {
            "event": "mcp_tool_call_metric",
            "tool": tool_name,
            "principal": principal_hash,
            "outcome": outcome,
            "duration_ms": round(duration_ms, 2),
        }
        if target is not None:
            data["target"] = target
        if extra:
            data.update(extra)

        logger.info(
            "mcp_tool_call_metric tool=%s principal=%s outcome=%s duration_ms=%.2f",
            tool_name,
            principal_hash,
            outcome,
            duration_ms,
            extra={"cat": "mcp", "data": data},
        )

        # Record into the in-memory rolling buffer powering the operator view.
        now = time.time()
        with _buffer_lock:
            _event_buffer.append((now, principal_hash, tool_name, outcome, float(duration_ms)))
            if len(_event_buffer) > _MAX_BUFFER_SIZE:
                # Drop oldest ~5% to amortise the cost and prevent unbounded
                # growth under a runaway client.
                drop_n = _MAX_BUFFER_SIZE // 20
                del _event_buffer[:drop_n]
    except Exception:
        # Best-effort — never let observability crash the tool.
        logger.debug("mcp_metrics: failed to emit record for tool=%s", tool_name, exc_info=True)


__all__ = ["record_tool_call", "snapshot_counters", "reset_counters"]
