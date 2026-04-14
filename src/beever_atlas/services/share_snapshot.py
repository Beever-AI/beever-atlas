"""Scrubber for shared conversation snapshots.

Positive allowlist: only `role`, `content`, `created_at` survive. Everything
else — `user_id`, `_id`, `source_id`, `embedding`, `raw_prompt`, `access_token`,
tool internals, citations — is dropped. This is defense-in-depth paired with
the regex tripwire in tests (`^(embedding|raw_prompt|.*_token|.*_secret|.*_key)$`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, str):
        return value
    return None


def build_share_snapshot(messages: list[dict]) -> list[dict]:
    """Return a new list with each message scrubbed to {role, content, created_at}.

    `created_at` falls back to `timestamp` for chat_history messages written
    with the legacy field name.
    """
    out: list[dict] = []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        created_at = _iso(m.get("created_at") or m.get("timestamp"))
        out.append(
            {
                "role": str(m.get("role", "")),
                "content": str(m.get("content", "")),
                "created_at": created_at,
            }
        )
    return out
