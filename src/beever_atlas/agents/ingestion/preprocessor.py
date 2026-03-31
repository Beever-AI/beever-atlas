"""Stage 1: PreprocessorAgent — filter and enrich raw messages.

Reads ``session.state["messages"]`` (list of raw NormalizedMessage dicts) and
writes ``session.state["preprocessed_messages"]`` (filtered, enriched list).

No LLM calls are made; this is a deterministic ``BaseAgent`` subclass.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# System message subtypes that carry no conversational content.
_SYSTEM_SUBTYPES: frozenset[str] = frozenset(
    {
        "channel_join",
        "channel_leave",
        "channel_topic",
        "channel_purpose",
        "channel_name",
        "channel_archive",
        "channel_unarchive",
        "group_join",
        "group_leave",
        "bot_add",
        "bot_remove",
        "pinned_item",
        "unpinned_item",
    }
)


def _is_skippable(msg: dict[str, Any]) -> bool:
    """Return True if the message should be excluded from preprocessing.

    Skipped if:
    - No ``text`` content (or text is purely whitespace).
    - Sent by a bot (``is_bot`` flag, ``bot_id`` present, or username matches known bot patterns).
    - Is a Slack join/leave or other system subtype.
    """
    text: str = (msg.get("text") or msg.get("content") or "").strip()
    if not text:
        return True

    raw_meta = msg.get("raw_metadata") if isinstance(msg.get("raw_metadata"), dict) else {}

    if (
        msg.get("is_bot")
        or msg.get("bot_id")
        or raw_meta.get("is_bot")
        or raw_meta.get("bot_id")
    ):
        return True

    # Catch bots that don't set is_bot flag (common with webhook-based bots).
    username = (
        msg.get("username") or msg.get("author_name") or msg.get("author") or ""
    ).lower()
    if username and any(
        pattern in username
        for pattern in ("bot", "helper", "webhook", "integration", "app")
    ):
        return True

    subtype: str = msg.get("subtype") or raw_meta.get("subtype") or ""
    if subtype in _SYSTEM_SUBTYPES:
        return True

    return False


def _detect_modality(msg: dict[str, Any]) -> str:
    """Return ``"mixed"`` when the message has file attachments, else ``"text"``."""
    files = msg.get("files") or []
    attachments = msg.get("attachments") or []
    if files or attachments:
        return "mixed"
    return "text"


def _message_key(msg: dict[str, Any]) -> str | None:
    """Return a stable message key for threading/context joins."""
    ts = msg.get("ts")
    if isinstance(ts, str) and ts:
        return ts
    message_id = msg.get("message_id")
    if isinstance(message_id, str) and message_id:
        return message_id
    timestamp = msg.get("timestamp")
    if isinstance(timestamp, datetime):
        return timestamp.isoformat()
    if isinstance(timestamp, str) and timestamp:
        return timestamp
    return None


def _coerce_timestamp_str(msg: dict[str, Any]) -> str:
    ts = msg.get("ts")
    if isinstance(ts, str) and ts:
        return ts
    timestamp = msg.get("timestamp")
    if isinstance(timestamp, datetime):
        return timestamp.isoformat()
    if isinstance(timestamp, str) and timestamp:
        return timestamp
    return ""


def _build_thread_context(
    msg: dict[str, Any],
    messages_by_ts: dict[str, dict[str, Any]],
) -> str | None:
    """Return a brief thread-context prefix for threaded replies.

    If the message is a reply (has ``thread_ts`` different from its own
    ``ts``), look up the parent in the current batch and return a summary
    string.  Returns ``None`` for top-level messages or when the parent is
    not in the batch.
    """
    thread_ts: str | None = msg.get("thread_ts") or msg.get("thread_id")
    msg_ts: str = _message_key(msg) or ""

    if not thread_ts or thread_ts == msg_ts:
        return None

    parent = messages_by_ts.get(thread_ts)
    if parent is None:
        return None

    parent_author: str = (
        parent.get("user")
        or parent.get("username")
        or parent.get("author")
        or parent.get("author_name")
        or "unknown"
    )
    parent_text: str = (parent.get("text") or parent.get("content") or "").strip()
    # Truncate long parent messages to keep context concise.
    if len(parent_text) > 200:
        parent_text = parent_text[:197] + "..."

    return f"[Reply to {parent_author}: {parent_text}]"


class PreprocessorAgent(BaseAgent):
    """Deterministic pre-processing stage for the ingestion pipeline.

    Reads ``session.state["messages"]``, applies filtering and enrichment,
    and writes the result to ``session.state["preprocessed_messages"]``.

    Each output message dict is the original dict with three additional keys:

    - ``modality``       — ``"text"`` or ``"mixed"``
    - ``thread_context`` — brief parent-message summary string, or ``None``
    - ``preprocessed``  — always ``True`` (sentinel for downstream stages)
    """

    model_config = {"arbitrary_types_allowed": True}

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        """Execute preprocessing and yield a single completion event."""
        messages: list[dict[str, Any]] = ctx.session.state.get("messages") or []
        sync_job_id = ctx.session.state.get("sync_job_id", "unknown")
        channel_id = ctx.session.state.get("channel_id", "unknown")
        batch_num = ctx.session.state.get("batch_num", "?")

        if not messages:
            logger.warning(
                "PreprocessorAgent: no messages job_id=%s channel=%s batch=%s; "
                "writing empty preprocessed_messages.",
                sync_job_id,
                channel_id,
                batch_num,
            )
            ctx.session.state["preprocessed_messages"] = []
            yield Event(author=self.name, invocation_id=ctx.invocation_id)
            return

        # Build a lookup map by ``ts`` for thread-context resolution.
        messages_by_ts: dict[str, dict[str, Any]] = {}
        for msg in messages:
            key = _message_key(msg)
            if key:
                messages_by_ts[key] = msg

        preprocessed: list[dict[str, Any]] = []
        skipped = 0

        for msg in messages:
            if _is_skippable(msg):
                skipped += 1
                continue

            enriched = dict(msg)
            # Normalize to prompt-expected keys while preserving the original payload.
            enriched["text"] = (msg.get("text") or msg.get("content") or "").strip()
            enriched["ts"] = _coerce_timestamp_str(msg)
            enriched["user"] = msg.get("user") or msg.get("author") or "unknown"
            enriched["username"] = (
                msg.get("username")
                or msg.get("author_name")
                or msg.get("author")
                or "unknown"
            )
            enriched["modality"] = _detect_modality(msg)
            enriched["thread_context"] = _build_thread_context(msg, messages_by_ts)
            enriched["preprocessed"] = True
            preprocessed.append(enriched)

        logger.info(
            "PreprocessorAgent: done job_id=%s channel=%s batch=%s in=%d skipped=%d retained=%d",
            sync_job_id,
            channel_id,
            batch_num,
            len(messages),
            skipped,
            len(preprocessed),
        )

        # Use state_delta so the change persists through InMemorySessionService
        # (direct ctx.session.state writes only modify a deep copy).
        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(
                state_delta={"preprocessed_messages": preprocessed},
            ),
        )
