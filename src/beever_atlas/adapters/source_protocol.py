"""Source protocols for the Message Store.

Defines the seam between content sources and the durable
``channel_messages`` collection. Two structurally-typed protocols cover
the two ingestion shapes:

* :class:`PullSource` — cursor-based, called by the sync runner. Existing
  platform adapters (Slack / Discord / Teams / etc.) are wrapped to satisfy
  this protocol.
* :class:`PushSource` — webhook-driven, called by an external system that
  pushes events. The OpenClaw / Hermes push-ingest endpoint registers as
  a ``PushSource``.

Both write to the Message Store; neither owns LLM extraction (that's the
ExtractionWorker's job). The split keeps interface segregation honest — a
``PullSource`` does not implement ``on_message_received``, and a
``PushSource`` does not implement ``fetch_and_persist``.

Spec:
``openspec/changes/oss-pipeline-and-wiki-redesign/specs/message-source-protocol/``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, Union, runtime_checkable


@runtime_checkable
class PullSource(Protocol):
    """A source that pulls messages from upstream by cursor.

    Implementations fetch a page of messages from the platform (Slack, Discord,
    Teams, Mattermost, file import, …), upsert them into the Message Store,
    and return the count persisted. The runner advances its cursor based on
    the persisted timestamps; the source itself is stateless beyond what the
    upstream platform exposes.
    """

    source_id: str

    async def fetch_and_persist(
        self,
        channel_id: str,
        since: datetime | None = None,
        max_messages: int = 1000,
    ) -> int:
        """Pull messages and upsert them to ``channel_messages``.

        Returns the number of new-or-updated rows persisted. Raises on fetch
        failure (no silent swallow); rows already persisted before a failure
        remain in the store as best-effort partial writes.
        """
        ...


@runtime_checkable
class PushSource(Protocol):
    """A source that receives messages via webhook / lifecycle hook.

    Implementations are typically registered HTTP endpoints (e.g. the
    OpenClaw / Hermes push-ingest endpoint). The handler validates the inbound
    event and calls ``on_message_received`` once per event, which upserts into
    ``channel_messages``.
    """

    source_id: str

    async def on_message_received(
        self,
        channel_id: str,
        message_id: str,
        payload: dict,
    ) -> None:
        """Handle a single pushed message; upsert to the Message Store.

        MUST be idempotent — repeated calls with the same ``(channel_id,
        message_id)`` MUST NOT produce duplicate rows. The compound unique
        index on the collection enforces this at the storage layer; the
        receiver SHOULD also guard with the per-source idempotency-key replay
        cache (24h TTL) to avoid the duplicate-then-noop round-trip.
        """
        ...


# Type alias — code that accepts either kind of source.
MessageSource = Union[PullSource, PushSource]


__all__ = ["PullSource", "PushSource", "MessageSource"]
