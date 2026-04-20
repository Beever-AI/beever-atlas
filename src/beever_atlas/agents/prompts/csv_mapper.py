"""Prompt for the CSV / JSONL column-mapping agent."""

from __future__ import annotations

import json
from typing import Iterable


CSV_MAPPER_INSTRUCTION = """You are a schema-mapping assistant. Your job: given the column \
headers of a chat-history file plus a few sample rows, identify which column supplies each \
canonical message field.

Canonical fields:
- content (REQUIRED): message body text
- author: stable author ID / handle
- author_name: display name (may equal author if only one author column exists)
- timestamp: date or datetime; set timestamp_time too if date and time live in separate columns
- message_id: unique message identifier
- thread_id: parent message / reply-to identifier
- attachments, reactions: optional

Rules:
1. Only use column names that appear EXACTLY in the provided headers list. Do not invent columns.
2. Use null when no column fits. Prefer null over a bad guess.
3. Return a confidence map in 0.0–1.0 for each non-null field.
4. Output STRICT JSON matching the schema. No prose, no markdown fences, no trailing text.

Expected JSON shape:
{
  "content": "<header or null>",
  "author": "<header or null>",
  "author_name": "<header or null>",
  "timestamp": "<header or null>",
  "timestamp_time": "<header or null>",
  "message_id": "<header or null>",
  "thread_id": "<header or null>",
  "attachments": "<header or null>",
  "reactions": "<header or null>",
  "confidence": {"content": 0.95, "timestamp": 0.9, ...},
  "detected_source": "<short description or null>",
  "notes": "<one sentence or empty string>"
}
"""


def build_user_prompt(
    filename: str,
    headers: Iterable[str],
    sample_rows: Iterable[dict[str, str]],
) -> str:
    """Construct the user-turn content with headers + samples."""
    headers_list = list(headers)
    samples_list = list(sample_rows)
    payload = {
        "filename": filename,
        "headers": headers_list,
        "sample_rows": samples_list[:3],
    }
    return (
        "Map the following file's columns to the canonical chat fields.\n\n"
        + json.dumps(payload, ensure_ascii=False, indent=2)
        + "\n\nRespond with strict JSON only."
    )
