"""Process-local ring buffer of recent LLM dispatch calls.

Surfaces a debug view of what dispatch actually sent to LiteLLM:
``(timestamp, consumer, provider, model, api_base, latency_ms, ok,
response_model, error)``. The ``/api/settings/debug/recent-llm-calls``
endpoint reads this so operators can confirm an Assignment switch
(e.g. "qa_agent → gemini-3.1-flash-lite") actually reached upstream.

Intentionally NOT persisted:
  * Request bodies (messages, embedding input). May contain PII; the
    debug surface is for routing confirmation, not transcript replay.
  * API keys (provider/api_base only, never the credential).
  * Full exception strings (only the exception class name + first 200
    chars, scrubbed via the credential redactor when applicable).

Size bound: 50 entries (~10KB). Process-local — restarts reset the log.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class RecentLLMCall:
    """One row in the recent-calls ring buffer."""

    ts: str
    """ISO-8601 timestamp the call started."""
    kind: str
    """``"completion"`` | ``"embedding"`` | ``"assignment"``."""
    consumer: str | None
    """Agent / consumer name for ``dispatch_assignment`` calls; else None."""
    provider: str
    """LiteLLM provider routed to (e.g. ``"openai"``, ``"gemini"``)."""
    model: str
    """LiteLLM model id sent on the wire."""
    api_base: str | None
    """Base URL configured for the call (no credential)."""
    latency_ms: int | None
    """Round-trip latency in milliseconds; None when the call raised."""
    ok: bool
    """True iff the dispatch returned without exception."""
    response_model: str | None
    """The ``model`` field echoed back by the upstream (when ok)."""
    error_class: str | None
    """Exception class name when ok=False."""
    error_summary: str | None
    """First ~200 chars of the exception message, credential-redacted."""


_RING_SIZE = 50
_recent: deque[RecentLLMCall] = deque(maxlen=_RING_SIZE)


def _now_iso() -> str:
    return datetime.now(tz=UTC).isoformat()


def _redact(text: str) -> str:
    """Apply the project's credential redactor; falls back to a noop on import error."""
    try:
        from beever_atlas.llm.endpoints import _redact_credential_fragments

        return _redact_credential_fragments(text)
    except Exception:  # noqa: BLE001
        return text


def record_call(
    *,
    started_at: float,
    kind: str,
    consumer: str | None,
    provider: str,
    model: str,
    api_base: str | None,
    response: Any | None = None,
    exc: BaseException | None = None,
) -> None:
    """Append one entry to the ring buffer. Never raises."""
    try:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        ok = exc is None
        response_model: str | None = None
        if ok and response is not None:
            # Defensive: a hostile / partial response object might raise from
            # a property accessor — recording must not fail just because we
            # can't read the echoed model name.
            try:
                response_model = getattr(response, "model", None)
            except Exception:  # noqa: BLE001
                response_model = None
            if response_model is None and isinstance(response, dict):
                response_model = response.get("model")
        error_class: str | None = None
        error_summary: str | None = None
        if exc is not None:
            error_class = type(exc).__name__
            error_summary = _redact(str(exc)[:200])
        _recent.append(
            RecentLLMCall(
                ts=_now_iso(),
                kind=kind,
                consumer=consumer,
                provider=provider,
                model=model,
                api_base=api_base,
                latency_ms=elapsed_ms if ok else None,
                ok=ok,
                response_model=response_model if isinstance(response_model, str) else None,
                error_class=error_class,
                error_summary=error_summary,
            )
        )
    except Exception:  # noqa: BLE001 — never crash dispatch on logging
        pass


def snapshot() -> list[dict[str, Any]]:
    """Return the ring buffer newest-first, serialised to dicts."""
    return [asdict(r) for r in reversed(_recent)]


def clear() -> None:
    """Reset the ring buffer (test fixtures)."""
    _recent.clear()


__all__ = ["RecentLLMCall", "record_call", "snapshot", "clear"]
