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
from typing import Any

logger = logging.getLogger(__name__)


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
    except Exception:
        # Best-effort — never let observability crash the tool.
        logger.debug(
            "mcp_metrics: failed to emit record for tool=%s", tool_name, exc_info=True
        )


__all__ = ["record_tool_call"]
