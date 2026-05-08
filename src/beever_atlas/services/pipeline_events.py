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
from dataclasses import dataclass
from datetime import UTC, datetime
from threading import Lock


@dataclass(frozen=True)
class Event:
    """One pipeline activity event."""

    ts: datetime
    stage: str
    label: str


class PipelineEventBuffer:
    """Channel-keyed ring buffer of recent pipeline events.

    Each channel keeps the most recent 100 events. Older entries are
    dropped silently (deque maxlen). Reads return most-recent-first so
    the UI can render a chronological activity feed without resorting
    on the client.
    """

    _MAX_PER_CHANNEL: int = 100

    def __init__(self) -> None:
        self._events: dict[str, deque[Event]] = {}
        self._lock = Lock()

    def record(
        self,
        channel_id: str,
        stage: str,
        label: str,
        ts: datetime | None = None,
    ) -> None:
        """Append one event for ``channel_id``.

        ``stage`` is a coarse pipeline phase (``fetch`` / ``preprocess``
        / ``extract`` / ``embed`` / ``persist`` / ``subbatch`` / ``wiki``
        / ``overview``). ``label`` is a human-readable description that
        the UI renders verbatim. ``ts`` defaults to ``datetime.now(UTC)``.
        """
        if not channel_id:
            return
        evt = Event(ts=ts or datetime.now(tz=UTC), stage=stage, label=label)
        with self._lock:
            bucket = self._events.get(channel_id)
            if bucket is None:
                bucket = deque(maxlen=self._MAX_PER_CHANNEL)
                self._events[channel_id] = bucket
            bucket.append(evt)

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
