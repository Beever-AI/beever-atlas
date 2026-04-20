"""Token-aware adaptive message batching.

Replaces fixed batch_size with token-budget-based splitting that prevents
output truncation and EOF errors. Preserves thread groups and prefers
time-window coherence.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)

# Projected output-token constants, used for the optional output budget.
# Tuned from current Pydantic schemas (see agents/schemas/extraction.py) — an
# ExtractedFact serialises to ~150 tokens, an ExtractedEntity to ~120.
AVG_TOKENS_PER_FACT = 180  # 20% headroom; re-tune from Phase 1.5 telemetry.
AVG_TOKENS_PER_ENTITY = 144  # 20% headroom; re-tune from Phase 1.5 telemetry.
EXPECTED_ENTITIES_PER_MESSAGE = 1  # empirical; most messages contribute ≤1 new entity.


def estimate_message_output_tokens(msg: dict[str, Any], max_facts_per_message: int = 2) -> int:
    """Estimate the output tokens a message will produce across both extractors.

    Used by ``token_aware_batches`` when an output-token budget is provided,
    to prevent batches whose projected response size would exceed the model's
    output ceiling.
    """
    fact_tokens = max_facts_per_message * AVG_TOKENS_PER_FACT
    entity_tokens = EXPECTED_ENTITIES_PER_MESSAGE * AVG_TOKENS_PER_ENTITY
    # 20-token overhead per message for JSON structure (brackets, commas, keys).
    return fact_tokens + entity_tokens + 20


def estimate_message_tokens(msg: dict[str, Any]) -> int:
    """Estimate the token count for a message using a 3-chars-per-token heuristic.

    Includes text, thread_context, media descriptions, and link metadata.
    """
    total_chars = 0

    # Message text (may include appended media descriptions and links)
    total_chars += len(msg.get("text") or msg.get("content") or "")

    # Thread context (injected by preprocessor)
    total_chars += len(msg.get("thread_context") or "")

    # Source link metadata
    for title in msg.get("source_link_titles") or []:
        total_chars += len(title or "")
    for desc in msg.get("source_link_descriptions") or []:
        total_chars += len(desc or "")

    # Raw metadata may contain additional content
    raw_meta = msg.get("raw_metadata")
    raw: dict[str, Any] = raw_meta if isinstance(raw_meta, dict) else {}
    for link in raw.get("links") or []:
        total_chars += len(link.get("title") or "")
        total_chars += len(link.get("description") or "")

    # Minimum 50 tokens for message overhead (author, ts, structural JSON)
    return max(total_chars // 3, 50)


def _get_message_timestamp(msg: dict[str, Any]) -> float:
    """Extract a float timestamp from a message for time-window grouping."""
    ts = msg.get("ts") or msg.get("timestamp")
    if isinstance(ts, (int, float)):
        return float(ts)
    if isinstance(ts, str) and ts:
        try:
            # Slack-style: "1234567890.123456"
            return float(ts.split(".")[0])
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            pass
    if isinstance(ts, datetime):
        return ts.timestamp()
    return 0.0


def _is_thread_reply(msg: dict[str, Any]) -> bool:
    """Return True if the message is a thread reply (not a parent)."""
    thread_id = msg.get("thread_ts") or msg.get("thread_id")
    msg_id = msg.get("ts") or msg.get("message_id")
    return bool(thread_id and thread_id != msg_id)


def token_aware_batches(
    messages: list[dict[str, Any]],
    max_tokens: int = 12000,
    time_window_seconds: int = 600,
    max_output_tokens: int | None = None,
    max_facts_per_message: int = 2,
    max_messages: int | None = None,
) -> list[list[dict[str, Any]]]:
    """Split messages into batches respecting a token budget.

    Algorithm:
    1. Sort messages by timestamp for chronological coherence
    2. Group into thread groups (parent + replies kept together)
    3. Accumulate thread groups into batches until budgets are reached
    4. Never split a thread group across batches

    Args:
        messages: List of message dicts (raw or preprocessed).
        max_tokens: Maximum estimated prompt tokens per batch.
        time_window_seconds: Preferred time window for grouping (secondary).
        max_output_tokens: Optional projected-output ceiling per batch. When
            provided, a batch also closes if adding the next thread group
            would cause projected response size to exceed this budget.
            ``None`` (default) preserves pre-existing input-only behavior.
        max_facts_per_message: Used to project output size. Defaults to 2 to
            match the balanced policy preset.

    Returns:
        List of message batches, each within the token budget.
    """
    if not messages:
        return []

    # Sort by timestamp for chronological order
    sorted_msgs = sorted(messages, key=_get_message_timestamp)

    # Build thread groups: each group is [parent, reply1, reply2, ...]
    # Messages without a thread_id are their own group.
    thread_groups: list[list[dict[str, Any]]] = []
    current_group: list[dict[str, Any]] = []

    for msg in sorted_msgs:
        if _is_thread_reply(msg):
            # Append to current group (reply follows parent)
            current_group.append(msg)
        else:
            # New top-level message — start a new group
            if current_group:
                thread_groups.append(current_group)
            current_group = [msg]

    if current_group:
        thread_groups.append(current_group)

    # Accumulate thread groups into batches within token budget
    batches: list[list[dict[str, Any]]] = []
    current_batch: list[dict[str, Any]] = []
    current_tokens = 0
    current_output_tokens = 0

    def _group_output(g: list[dict[str, Any]]) -> int:
        return sum(estimate_message_output_tokens(m, max_facts_per_message) for m in g)

    for group in thread_groups:
        group_tokens = sum(estimate_message_tokens(m) for m in group)
        group_output = _group_output(group) if max_output_tokens is not None else 0

        # If this single group exceeds either budget, it gets its own batch.
        oversized_input = group_tokens >= max_tokens
        oversized_output = max_output_tokens is not None and group_output >= max_output_tokens
        if oversized_input or oversized_output:
            if current_batch:
                batches.append(current_batch)
                current_batch = []
                current_tokens = 0
                current_output_tokens = 0
            batches.append(group)
            logger.info(
                "AdaptiveBatcher: oversized thread group (%d in, %d out, %d msgs) in own batch",
                group_tokens,
                group_output,
                len(group),
            )
            continue

        # Hard message-count cap (evaluated first so smallest limit wins).
        if (
            max_messages is not None
            and len(current_batch) + len(group) > max_messages
            and current_batch
        ):
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
            current_output_tokens = 0

        # Would adding this group exceed input OR output budget?
        would_overflow_input = current_tokens + group_tokens > max_tokens
        would_overflow_output = (
            max_output_tokens is not None
            and current_output_tokens + group_output > max_output_tokens
        )
        if (would_overflow_input or would_overflow_output) and current_batch:
            batches.append(current_batch)
            current_batch = []
            current_tokens = 0
            current_output_tokens = 0

        current_batch.extend(group)
        current_tokens += group_tokens
        current_output_tokens += group_output

    if current_batch:
        batches.append(current_batch)

    if batches:
        sizes = [len(b) for b in batches]
        token_ests = [sum(estimate_message_tokens(m) for m in b) for b in batches]
        if max_output_tokens is not None:
            output_ests = [
                sum(estimate_message_output_tokens(m, max_facts_per_message) for m in b)
                for b in batches
            ]
            logger.info(
                "AdaptiveBatcher: %d messages → %d batches (sizes=%s, in_tokens=%s, out_tokens=%s)",
                len(messages),
                len(batches),
                sizes,
                token_ests,
                output_ests,
            )
        else:
            logger.info(
                "AdaptiveBatcher: %d messages → %d batches (sizes=%s, tokens=%s)",
                len(messages),
                len(batches),
                sizes,
                token_ests,
            )

    return batches
