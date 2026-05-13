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


def register_litellm_observer() -> None:
    """Hook LiteLLM's success/failure callbacks to record every call.

    PR-λ.7: ``dispatch_completion`` / ``dispatch_assignment`` only see a
    subset of LLM traffic — agent code (qa_agent, fact_extractor, …) uses
    Google ADK's ``LiteLlm`` wrapper which calls ``litellm.acompletion``
    directly, bypassing our dispatch wrappers entirely. As a result the
    in-row "Last call" indicator never lit up for agent traffic.

    LiteLLM's own success_callback / failure_callback hooks fire on every
    request regardless of who initiated it, so registering a callback here
    catches the full picture: dispatch wrappers, Google ADK calls, the
    Test Connection probe, the re-embed migration, everything.

    Idempotent — re-registering at boot in tests / hot reload is safe.
    """
    try:
        import litellm  # type: ignore[import-untyped]
    except Exception:  # noqa: BLE001
        return

    def _record_from_kwargs(
        kwargs: dict[str, Any],
        response: Any | None,
        exc: BaseException | None,
        start_time: Any,
        end_time: Any,
    ) -> None:
        try:
            import time as _time

            # Translate litellm's wall-clock times into a monotonic offset for
            # the recorder. When start_time is missing fall back to now-elapsed
            # so latency_ms is at least non-None for successes.
            started_at = _time.monotonic()
            if start_time is not None and end_time is not None:
                try:
                    elapsed = float(end_time - start_time) if hasattr(end_time, "__sub__") else 0.0
                    started_at = _time.monotonic() - elapsed
                except Exception:  # noqa: BLE001
                    pass

            provider = (
                kwargs.get("custom_llm_provider")
                or kwargs.get("litellm_provider")
                or ""
            )
            model = kwargs.get("model") or ""
            api_base = kwargs.get("api_base") if isinstance(kwargs.get("api_base"), str) else None
            consumer: str | None = None
            metadata = kwargs.get("metadata") if isinstance(kwargs.get("metadata"), dict) else None
            if metadata:
                v = metadata.get("consumer")
                if isinstance(v, str):
                    consumer = v
            record_call(
                started_at=started_at,
                kind="completion",
                consumer=consumer,
                provider=str(provider),
                model=str(model),
                api_base=api_base,
                response=response,
                exc=exc,
            )
        except Exception:  # noqa: BLE001 — never crash the callback
            pass

    def _on_success(kwargs: dict[str, Any], response: Any, start_time: Any, end_time: Any) -> None:
        _record_from_kwargs(kwargs, response, None, start_time, end_time)

    def _on_failure(kwargs: dict[str, Any], exc: BaseException, start_time: Any, end_time: Any) -> None:
        _record_from_kwargs(kwargs, None, exc, start_time, end_time)

    # Avoid duplicate registration on hot reload — LiteLLM allows multiple
    # but we only want one record per call.
    if not any(getattr(cb, "__name__", "") == "_on_success" and getattr(cb, "__module__", "") == __name__ for cb in (litellm.success_callback or [])):
        litellm.success_callback.append(_on_success)
    if not any(getattr(cb, "__name__", "") == "_on_failure" and getattr(cb, "__module__", "") == __name__ for cb in (litellm.failure_callback or [])):
        litellm.failure_callback.append(_on_failure)


__all__ = ["RecentLLMCall", "record_call", "snapshot", "clear", "register_litellm_observer"]
