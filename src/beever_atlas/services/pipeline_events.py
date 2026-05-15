"""In-memory pipeline event ring buffer for the activity feed.

Phase 0 / Task 1.3 of ``sync-pipeline-feedback-and-auto-wiki``: hook the
existing log lines (fetch / preprocess / extract / embed / persist /
sub-batch start+done) into a shared, channel-keyed ring so the API
extension in Phase 3 (D6) can surface a ``recent_events`` list without a
new transport. Buffer is intentionally process-local — no durability.

The event dataclass is read-only and JSON-serialisable so callers can
drop it directly into an HTTP response. Module-level singleton keeps the
ring shared between the BatchProcessor (sub-batch events), the
ExtractionWorker (tick boundaries), and any future maintainer / overview
emitters without threading a reference through every constructor.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock
from typing import Any


# Event-type taxonomy (``unified-llm-wiki-graph-redesign``):
#
#   * ``message_processing`` — one message currently being processed by
#     ingestion. Payload: ``{message_id, text_preview, author, ts}``.
#   * ``agent_state`` — one ingestion agent transition. Payload:
#     ``{agent, state, batch_id?, elapsed_ms?}``.
#   * ``wiki_update`` — one wiki page write. Payload:
#     ``{page_id, action, facts_integrated?, elapsed_ms?}``.
#   * ``cost_summary`` — per-build / per-flush cost. Payload:
#     ``{calls_total, calls_skipped?, input_tokens, output_tokens, usd}``.
#   * ``parse_failure`` — LLM response parse failure. Payload:
#     ``{page_id, raw_len}``.
#
# Legacy callers that only emit ``stage`` + ``label`` keep working
# unchanged — ``payload`` defaults to ``None`` and the existing
# ``recent_events`` API skips it when serialising.
EVENT_TYPE_MESSAGE_PROCESSING = "message_processing"
EVENT_TYPE_AGENT_STATE = "agent_state"
EVENT_TYPE_WIKI_UPDATE = "wiki_update"
EVENT_TYPE_COST_SUMMARY = "cost_summary"
EVENT_TYPE_PARSE_FAILURE = "parse_failure"


@dataclass(frozen=True)
class Event:
    """One pipeline activity event."""

    ts: datetime
    stage: str
    label: str
    event_type: str = "legacy"
    payload: dict[str, Any] | None = field(default=None)


class PipelineEventBuffer:
    """Channel-keyed ring buffer of recent pipeline events.

    Each channel keeps the most recent 200 events. Older entries are
    dropped silently (deque maxlen). Reads return most-recent-first so
    the UI can render a chronological activity feed without resorting
    on the client.

    The redesign extends the buffer with structured event types
    (``message_processing``, ``agent_state``, ``wiki_update``,
    ``cost_summary``, ``parse_failure``) so the SyncMonitor's three
    panes can subscribe to the same buffer instead of running their
    own counters.
    """

    _MAX_PER_CHANNEL: int = 200

    def __init__(self) -> None:
        self._events: dict[str, deque[Event]] = {}
        self._lock = Lock()
        # Rolling 10-minute parse-failure counter per channel — used by
        # the WikiTab parse-failure banner. List of monotonic-ish
        # timestamps (we use wall-clock ts for simplicity; close enough
        # for a 10-min rolling window).
        self._parse_failure_ts: dict[str, deque[datetime]] = {}

    def record(
        self,
        channel_id: str,
        stage: str,
        label: str,
        ts: datetime | None = None,
        event_type: str = "legacy",
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Append one event for ``channel_id``.

        ``stage`` is a coarse pipeline phase. ``label`` is a
        human-readable description. ``event_type`` is the structured
        taxonomy slot (default ``legacy`` preserves old emitters).
        ``payload`` carries event-type-specific structured data the
        SyncMonitor consumes verbatim. ``ts`` defaults to now-UTC.
        """
        if not channel_id:
            return
        evt = Event(
            ts=ts or datetime.now(tz=UTC),
            stage=stage,
            label=label,
            event_type=event_type,
            payload=payload,
        )
        with self._lock:
            bucket = self._events.get(channel_id)
            if bucket is None:
                bucket = deque(maxlen=self._MAX_PER_CHANNEL)
                self._events[channel_id] = bucket
            bucket.append(evt)
            if event_type == EVENT_TYPE_PARSE_FAILURE:
                fail_bucket = self._parse_failure_ts.setdefault(channel_id, deque(maxlen=200))
                fail_bucket.append(evt.ts)

    def parse_failure_count_last_10_min(self, channel_id: str) -> int:
        """Count parse-failure events for the channel in the last 10 min.

        Used by the WikiTab banner: when count ≥3, render the failure
        banner with Retry / Dismiss / Details actions.
        """
        with self._lock:
            bucket = self._parse_failure_ts.get(channel_id)
            if not bucket:
                return 0
            cutoff = datetime.now(tz=UTC).timestamp() - 600.0
            return sum(1 for ts in bucket if ts.timestamp() >= cutoff)

    def recent_for(self, channel_id: str, limit: int = 10) -> list[Event]:
        """Return up to ``limit`` events for ``channel_id``, newest first."""
        with self._lock:
            bucket = self._events.get(channel_id)
            if not bucket:
                return []
            # Iterate newest-first; deque has no negative indexing slice
            # that returns a list, so reverse-iterate explicitly.
            out: list[Event] = []
            for evt in reversed(bucket):
                out.append(evt)
                if len(out) >= max(0, limit):
                    break
            return out

    def clear(self, channel_id: str | None = None) -> None:
        """Drop all events for one channel, or all channels when ``None``."""
        with self._lock:
            if channel_id is None:
                self._events.clear()
            else:
                self._events.pop(channel_id, None)


_buffer_singleton: PipelineEventBuffer | None = None


def get_pipeline_events() -> PipelineEventBuffer:
    """Return the process-wide :class:`PipelineEventBuffer` singleton."""
    global _buffer_singleton
    if _buffer_singleton is None:
        _buffer_singleton = PipelineEventBuffer()
    return _buffer_singleton


def emit_agent_state(
    channel_id: str,
    agent: str,
    state: str,
    *,
    batch_id: str | None = None,
    elapsed_ms: int | None = None,
    error_class: str | None = None,
) -> None:
    """Best-effort ``agent_state`` event emit for SyncMonitor LEDs.

    Wraps the ring-buffer write in a defensive try/except so an event-buffer
    hiccup never crashes the agent. ``state`` is ``running`` / ``done`` /
    ``failed`` per design D1.
    """
    try:
        payload: dict[str, Any] = {"agent": agent, "state": state}
        if batch_id is not None:
            payload["batch_id"] = batch_id
        if elapsed_ms is not None:
            payload["elapsed_ms"] = elapsed_ms
        if error_class is not None:
            payload["error_class"] = error_class
        label = f"{agent} {state}"
        if elapsed_ms is not None:
            label = f"{label} ({elapsed_ms}ms)"
        get_pipeline_events().record(
            channel_id=channel_id,
            stage="agent",
            label=label,
            event_type=EVENT_TYPE_AGENT_STATE,
            payload=payload,
        )
    except Exception:  # noqa: BLE001 — observability must never break the agent
        pass


def emit_message_processing(
    channel_id: str,
    *,
    message_id: str,
    text_preview: str,
    author: str,
    ts: datetime | None = None,
) -> None:
    """Best-effort ``message_processing`` event emit for SyncMonitor stream.

    ``text_preview`` is truncated to 200 chars per design D1 to bound the
    SSE payload size.
    """
    try:
        preview = (text_preview or "")[:200]
        get_pipeline_events().record(
            channel_id=channel_id,
            stage="message",
            label=f"Processing {message_id[:32]}",
            event_type=EVENT_TYPE_MESSAGE_PROCESSING,
            payload={
                "message_id": message_id,
                "text_preview": preview,
                "author": author,
                "ts": ts.isoformat() if ts else None,
            },
        )
    except Exception:  # noqa: BLE001
        pass
