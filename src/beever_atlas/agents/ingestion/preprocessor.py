"""Stage 1: PreprocessorAgent — filter and enrich raw messages.

Reads ``session.state["messages"]`` (list of raw NormalizedMessage dicts) and
writes ``session.state["preprocessed_messages"]`` (filtered, enriched list).

No LLM calls are made; this is a deterministic ``BaseAgent`` subclass.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from beever_atlas.infra.config import get_settings

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


# ── Slack mrkdwn fallback cleaner ────────────────────────────────────────────
# The bridge (TypeScript) does primary cleaning. This is a safety net so the
# LLM never sees raw Slack markup even if the bridge is bypassed or misses
# an edge case.

_SLACK_LINK_RE = re.compile(r"<([^>]+)>")
_HTML_ENTITIES = {"&amp;": "&", "&lt;": "<", "&gt;": ">"}


def _clean_slack_text(text: str) -> str:
    """Strip residual Slack mrkdwn markup from message text.

    Handles ``<url|label>`` links, ``<@U123>`` mentions, ``<#C123|name>``
    channel refs, ``<!here>``-style special mentions, and HTML entities.
    """
    if not text:
        return text

    def _replace_bracket(m: re.Match[str]) -> str:
        inner = m.group(1)
        # User mention: <@U123> or <@U123|name>
        if inner.startswith("@"):
            parts = inner.split("|", 1)
            return f"@{parts[1]}" if len(parts) > 1 else f"@{inner[1:]}"
        # Channel mention: <#C123|name>
        if inner.startswith("#"):
            parts = inner.split("|", 1)
            return f"#{parts[1]}" if len(parts) > 1 else f"#{inner[1:]}"
        # Special: <!here>, <!channel>, <!everyone>, <!subteam^...|@group>
        if inner.startswith("!"):
            parts = inner.split("|", 1)
            if len(parts) > 1:
                return parts[1]
            keyword = inner[1:].split("^")[0]
            return f"@{keyword}"
        # URL with label: <url|label>
        if "|" in inner:
            return inner.split("|", 1)[1]
        # Bare URL
        return inner

    cleaned = _SLACK_LINK_RE.sub(_replace_bracket, text)

    # Decode HTML entities
    for entity, char in _HTML_ENTITIES.items():
        cleaned = cleaned.replace(entity, char)

    return cleaned


# Word-boundary pattern for bot-like usernames. Matches "deploy-bot", "ci_bot",
# "bot-notifier" but NOT "abbott", "robotics", "appleton".
_BOT_USERNAME_RE = re.compile(r"(?:^|[-_])bot(?:[-_]|$)|^webhook$|^integration$", re.IGNORECASE)


def _is_bot_message(msg: dict[str, Any]) -> bool:
    """Return True if the message appears to be from a bot."""
    raw_meta = msg.get("raw_metadata") if isinstance(msg.get("raw_metadata"), dict) else {}

    if msg.get("is_bot") or msg.get("bot_id") or raw_meta.get("is_bot") or raw_meta.get("bot_id"):
        return True

    username = msg.get("username") or msg.get("author_name") or msg.get("author") or ""
    if username and _BOT_USERNAME_RE.search(username):
        return True

    return False


def _is_skippable(msg: dict[str, Any]) -> bool:
    """Return True if the message should be excluded from preprocessing.

    Skipped if:
    - No ``text`` content (or text is purely whitespace).
    - Sent by a bot — UNLESS it's a thread reply with substantive content (>20 chars).
    - Is a Slack join/leave or other system subtype.
    """
    text: str = (msg.get("text") or msg.get("content") or "").strip()
    has_attachments = bool(msg.get("attachments") or msg.get("files"))
    if not text and not has_attachments:
        return True

    raw_meta = msg.get("raw_metadata") if isinstance(msg.get("raw_metadata"), dict) else {}

    if _is_bot_message(msg):
        # Keep bot thread replies with substantive content (CI results, deploy notices, etc.)
        thread_id = msg.get("thread_ts") or msg.get("thread_id")
        msg_id = msg.get("ts") or msg.get("message_id")
        is_thread_reply = thread_id and thread_id != msg_id
        if is_thread_reply and len(text) > 20:
            pass  # Keep this bot reply — it has substance
        else:
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


async def _resolve_cross_batch_parent(
    msg: dict[str, Any],
    messages_by_ts: dict[str, dict[str, Any]],
) -> str | None:
    """Resolve thread parent from persisted stores when not in current batch.

    Falls back to MongoDB then Weaviate for parent message lookup.
    """
    settings = get_settings()

    if not settings.cross_batch_thread_context_enabled:
        return None

    thread_ts: str | None = msg.get("thread_ts") or msg.get("thread_id")
    msg_ts: str = _message_key(msg) or ""

    if not thread_ts or thread_ts == msg_ts:
        return None

    # Already resolved in-batch
    if thread_ts in messages_by_ts:
        return None

    max_len = settings.thread_context_max_length

    try:
        from beever_atlas.stores import get_stores

        stores = get_stores()

        # Try MongoDB first
        record = await stores.mongodb.db["raw_messages"].find_one({"message_ts": thread_ts})
        if record:
            author = record.get("author_name") or record.get("author") or "unknown"
            text = (record.get("text") or record.get("content") or "").strip()
            if len(text) > max_len:
                text = text[: max_len - 3] + "..."
            return f"[Reply to {author}: {text}]"

        # Fallback: search Weaviate for a fact from this message
        weaviate_facts = await stores.weaviate.list_facts(
            channel_id=msg.get("channel_id", ""),
            filters=type(
                "F",
                (),
                {"topic": None, "entity": None, "importance": None, "since": None, "until": None},
            )(),
            page=1,
            limit=1,
        )
        if weaviate_facts:
            first = weaviate_facts[0] if isinstance(weaviate_facts, list) else None
            if first:
                text = (first.get("text") or first.get("statement") or "")[:max_len]
                if text:
                    return f"[Reply to thread: {text}]"

    except Exception:
        logger.warning(
            "PreprocessorAgent: cross-batch thread lookup failed for thread_ts=%s",
            thread_ts,
            exc_info=True,
        )

    return None


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
        from beever_atlas.agents.callbacks.checkpoint_skip import should_skip_stage

        if should_skip_stage(ctx.session.state, "preprocessed_messages", self.name):
            yield Event(author=self.name, invocation_id=ctx.invocation_id)
            return

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
        msg_seq = 0

        for msg in messages:
            if _is_skippable(msg):
                skipped += 1
                continue

            enriched = dict(msg)
            # Normalize to prompt-expected keys while preserving the original payload.
            raw_text = (msg.get("text") or msg.get("content") or "").strip()
            enriched["text"] = _clean_slack_text(raw_text)
            enriched["ts"] = _coerce_timestamp_str(msg)
            enriched["user"] = msg.get("user") or msg.get("author") or "unknown"
            enriched["username"] = (
                msg.get("username") or msg.get("author_name") or msg.get("author") or "unknown"
            )
            enriched["modality"] = _detect_modality(msg)
            enriched["thread_context"] = _build_thread_context(msg, messages_by_ts)
            enriched["preprocessed"] = True
            enriched["msg_id"] = f"msg-{msg_seq}"
            msg_seq += 1
            preprocessed.append(enriched)

        # --- Coreference resolution ---
        coref_settings = get_settings()
        if coref_settings.coref_enabled and preprocessed:
            try:
                from beever_atlas.services.coreference_resolver import (
                    fetch_channel_history,
                    resolve_coreferences,
                )

                history = await fetch_channel_history(
                    channel_id, limit=coref_settings.coref_history_limit
                )
                await resolve_coreferences(preprocessed, history)
            except Exception:
                logger.warning(
                    "PreprocessorAgent: coreference resolution failed job_id=%s",
                    sync_job_id,
                    exc_info=True,
                )

        # --- Cross-batch thread context ---
        for enriched_msg in preprocessed:
            if enriched_msg.get("thread_context") is None:
                cross_batch_ctx = await _resolve_cross_batch_parent(enriched_msg, messages_by_ts)
                if cross_batch_ctx:
                    enriched_msg["thread_context"] = cross_batch_ctx

        # Process media attachments and extract links for all messages.
        media_enriched = 0
        mixed_msgs: list[dict[str, Any]] = []
        for enriched_msg in preprocessed:
            # --- Extract link/unfurl metadata for ALL messages ---
            raw_meta = (
                enriched_msg.get("raw_metadata")
                if isinstance(enriched_msg.get("raw_metadata"), dict)
                else {}
            )
            links = raw_meta.get("links") or enriched_msg.get("links") or []

            # Also extract bare URLs from message text (not already in unfurls)
            unfurl_urls = {link.get("url", "") for link in links}
            text_urls = re.findall(r"https?://[^\s<>\]|)]+", enriched_msg.get("text", ""))
            for url in text_urls:
                # Strip trailing punctuation
                url = url.rstrip(".,;:!?")
                if url and url not in unfurl_urls:
                    links.append({"url": url, "title": "", "description": ""})

            if links:
                link_urls: list[str] = []
                link_titles: list[str] = []
                link_descriptions: list[str] = []
                link_text_parts: list[str] = []
                for link in links:
                    url = link.get("url", "")
                    title = link.get("title", "")
                    description = link.get("description", "")
                    if url:
                        link_urls.append(url)
                        link_titles.append(title)
                        link_descriptions.append(description)
                        if title or description:
                            link_text_parts.append(
                                f"[Link: {title} — {description} ({url})]"
                                if description
                                else f"[Link: {title} ({url})]"
                            )
                enriched_msg["source_link_urls"] = link_urls
                enriched_msg["source_link_titles"] = link_titles
                enriched_msg["source_link_descriptions"] = link_descriptions
                if link_text_parts:
                    enriched_msg["text"] += "\n\n" + "\n".join(link_text_parts)

            if enriched_msg.get("modality") != "mixed":
                continue

            # Always capture attachment URLs and names before media processing (so they survive failures)
            attachments = enriched_msg.get("attachments") or []
            files = enriched_msg.get("files") or []
            all_atts = attachments + files
            fallback_urls = [
                a.get("url") or a.get("url_private") or ""
                for a in all_atts
                if a.get("url") or a.get("url_private")
            ]
            fallback_names = [
                a.get("name") or a.get("title") or ""
                for a in all_atts
                if a.get("url") or a.get("url_private")
            ]
            if fallback_urls and not enriched_msg.get("source_media_urls"):
                enriched_msg["source_media_urls"] = fallback_urls
                enriched_msg["source_media_names"] = fallback_names
                # Detect type from first attachment
                first_att = all_atts[0] if all_atts else {}
                att_type = first_att.get("type", "")
                enriched_msg["source_media_type"] = (
                    att_type if att_type in ("image", "pdf", "video") else "file"
                )

            mixed_msgs.append(enriched_msg)

        # Concurrent media processing: fire off all API calls in parallel,
        # then apply results sequentially to preserve virtual-message ordering.
        if mixed_msgs:
            import asyncio as _asyncio
            from beever_atlas.services.media_processor import MediaProcessor

            media_processor = MediaProcessor()

            async def _safe_process(msg: dict[str, Any]) -> dict[str, Any] | None:
                try:
                    return await media_processor.process_message_media(msg)
                except Exception:
                    logger.warning(
                        "PreprocessorAgent: media processing failed for message %s",
                        msg.get("ts"),
                        exc_info=True,
                    )
                    return None

            media_results = await _asyncio.gather(*[_safe_process(m) for m in mixed_msgs])
            await media_processor.close()

            for enriched_msg, media_result in zip(mixed_msgs, media_results):
                if media_result is None:
                    continue

                if media_result.get("description"):
                    desc = media_result["description"]
                    # Cap media descriptions to avoid bloating the fact extraction prompt
                    if len(desc) > 2000:
                        desc = desc[:2000] + "\n[...truncated]"
                    enriched_msg["text"] += "\n\n" + desc
                    media_enriched += 1
                if media_result.get("media_urls"):
                    enriched_msg["source_media_urls"] = media_result["media_urls"]
                    enriched_msg["source_media_type"] = media_result["media_type"]

        logger.info(
            "PreprocessorAgent: done job_id=%s channel=%s batch=%s in=%d skipped=%d retained=%d media_enriched=%d",
            sync_job_id,
            channel_id,
            batch_num,
            len(messages),
            skipped,
            len(preprocessed),
            media_enriched,
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
