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

import contextvars
import logging
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse, urlunparse

logger = logging.getLogger(__name__)


# Set to True by ``services/llm_dispatch.dispatch_completion`` /
# ``dispatch_embedding`` / ``dispatch_assignment`` while they're running their
# own ``record_call``. The LiteLLM CustomLogger checks this flag and skips
# recording, so the same dispatched call doesn't land in the ring buffer twice.
#
# Per-task context — set/reset via ``contextvars.ContextVar.set``/``.reset``
# so concurrent dispatch calls on different asyncio tasks each see their own
# value. The CustomLogger callback fires from within ``litellm.acompletion``'s
# await frame, which runs in the same task that called dispatch, so the flag
# is visible at the right moment.
_DISPATCH_OWNS_RECORDING: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_beever_dispatch_owns_recording", default=False
)


def _strip_url_credentials(url: str) -> str:
    """Remove ``user:pass@`` from a URL before it lands in the ring buffer.

    Operators sometimes configure an Endpoint ``base_url`` with embedded
    HTTP Basic credentials (corporate proxy, signed-tunnel forwarder, etc.).
    Without this scrub, those credentials would appear in the
    ``/api/settings/debug/recent-llm-calls`` response and in operator log
    lines. Returns the URL unchanged when no userinfo is present.
    """
    try:
        parsed = urlparse(url)
        if parsed.username or parsed.password:
            host = parsed.hostname or ""
            netloc = f"{host}:{parsed.port}" if parsed.port else host
            return urlunparse(parsed._replace(netloc=netloc))
    except Exception:  # noqa: BLE001 — bad URLs fall through to original
        pass
    return url


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
            # LiteLLM's callback contract passes ``start_time`` / ``end_time``
            # as ``datetime`` objects in UTC. ``end - start`` returns a
            # ``timedelta`` — float() on that raises TypeError and falls
            # through, leaving the recorded latency at 0ms. Convert via
            # ``.total_seconds()`` so the operator-visible log line shows
            # real timing (was the cause of every "latency_ms=0" line).
            started_at = _time.monotonic()
            elapsed_seconds = 0.0
            if start_time is not None and end_time is not None:
                try:
                    diff = end_time - start_time
                    if hasattr(diff, "total_seconds"):
                        elapsed_seconds = float(diff.total_seconds())
                    else:
                        elapsed_seconds = float(diff)
                    started_at = _time.monotonic() - elapsed_seconds
                except Exception:  # noqa: BLE001
                    pass

            # LiteLLM exposes the call's effective routing under
            # ``kwargs["litellm_params"]`` (a normalised mirror), not at the
            # top level. The top-level ``api_base`` is only present when
            # the caller passed it as a direct kwarg AND LiteLLM hasn't
            # promoted it into litellm_params yet — for the ADK path
            # (qa_agent etc.) the top-level slot is always None. Prefer
            # the nested copy so the ring buffer reflects where the call
            # actually went on the wire.
            litellm_params = (
                kwargs.get("litellm_params")
                if isinstance(kwargs.get("litellm_params"), dict)
                else None
            )
            provider = (
                kwargs.get("custom_llm_provider")
                or (litellm_params.get("custom_llm_provider") if litellm_params else None)
                or kwargs.get("litellm_provider")
                or ""
            )
            model = kwargs.get("model") or ""
            api_base_top = kwargs.get("api_base")
            api_base_nested = litellm_params.get("api_base") if litellm_params else None
            api_base_raw = (
                api_base_top
                if isinstance(api_base_top, str)
                else api_base_nested
                if isinstance(api_base_nested, str)
                else None
            )
            # LiteLLM often appends a trailing "/" to the nested copy
            # (``https://api.z.ai/api/paas/v4/`` vs the operator-saved
            # ``…/v4``). Strip so the ring buffer matches what the UI
            # compares against in lastByModel. Also strip any embedded
            # ``user:pass@`` userinfo so operators who proxy through
            # ``https://x:y@proxy/v1`` don't leak the credential into
            # the debug surface.
            api_base = (
                _strip_url_credentials(api_base_raw.rstrip("/"))
                if isinstance(api_base_raw, str)
                else None
            )
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

            # Surface every LiteLLM call to the standard logger so operators
            # can SEE which model handled a turn, how long it took, and
            # whether it failed — without having to poll the ring buffer.
            # Visible in uvicorn's console at INFO. Credentials never appear
            # here because we only forward (provider, model, base, error
            # class + redacted summary).
            try:
                latency_ms = int((_time.monotonic() - started_at) * 1000)
                stream = kwargs.get("stream")
                if exc is None:
                    logger.info(
                        "llm call ok: consumer=%s provider=%s model=%s base=%s "
                        "latency_ms=%d stream=%s",
                        consumer or "-",
                        provider or "-",
                        model or "-",
                        api_base or "-",
                        latency_ms,
                        bool(stream),
                    )
                else:
                    summary = _redact(str(exc)[:200])
                    logger.warning(
                        "llm call FAIL: consumer=%s provider=%s model=%s base=%s "
                        "stream=%s error_class=%s error_summary=%s",
                        consumer or "-",
                        provider or "-",
                        model or "-",
                        api_base or "-",
                        bool(stream),
                        type(exc).__name__,
                        summary,
                    )
            except Exception:  # noqa: BLE001 — logging must never crash
                pass
        except Exception:  # noqa: BLE001 — never crash the callback
            pass

    # LiteLLM's ``CustomLogger`` API fires for BOTH streamed and non-streamed
    # completions, so it is the single source of truth for the ring buffer.
    # The legacy function-style ``success_callback`` / ``failure_callback`` is
    # NOT registered here — it would fire alongside ``CustomLogger`` for every
    # non-streamed call, producing duplicate entries (and the function-style
    # variant misses streamed calls entirely anyway).
    try:
        from litellm.integrations.custom_logger import CustomLogger  # type: ignore[import-untyped]
    except Exception:  # noqa: BLE001
        return

    class _RingBufferLogger(CustomLogger):  # type: ignore[misc]
        async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401, ANN001
            # ``dispatch_completion`` / ``dispatch_embedding`` /
            # ``dispatch_assignment`` each call ``record_call`` directly so
            # they can capture circuit-breaker and 429 sniffing state at the
            # exact moment the dispatch returns. When this CustomLogger fires
            # from inside that same dispatch path, skip — the dispatch wrapper
            # has already written the canonical entry. Without this guard,
            # every non-ADK call would record twice (dispatch + CustomLogger).
            if _DISPATCH_OWNS_RECORDING.get():
                return
            _record_from_kwargs(kwargs, response_obj, None, start_time, end_time)

        async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):  # noqa: D401, ANN001
            # See success-event docstring — same dispatch-owns-recording
            # guard so a failed dispatch call doesn't record twice.
            if _DISPATCH_OWNS_RECORDING.get():
                return
            # CustomLogger's failure hook receives the exception via
            # ``response_obj`` (it carries the upstream error object). The
            # nested ``kwargs["exception"]`` is the canonical exception
            # when present.
            exc = (
                kwargs.get("exception")
                if isinstance(kwargs.get("exception"), BaseException)
                else (response_obj if isinstance(response_obj, BaseException) else None)
            )
            _record_from_kwargs(kwargs, None, exc, start_time, end_time)

    already = any(isinstance(cb, _RingBufferLogger) for cb in (litellm.callbacks or []))
    if not already:
        # The ``callbacks`` registry accepts CustomLogger subclass instances
        # and fans events to all of them. Idempotent on re-register.
        if litellm.callbacks is None:
            litellm.callbacks = []
        litellm.callbacks.append(_RingBufferLogger())


__all__ = ["RecentLLMCall", "record_call", "snapshot", "clear", "register_litellm_observer"]
