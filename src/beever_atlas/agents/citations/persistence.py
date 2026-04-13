"""Persistence helpers for the citation envelope.

Both `ChatHistoryStore.messages[].citations` and `QAHistoryStore.citations_json`
transition from a bare `list[dict]` (legacy) to the envelope shape
`{items: list[dict], sources: list[dict], refs: list[dict]}`.

These helpers let store code convert both directions without caring which
regime wrote the row.
"""

from __future__ import annotations

from typing import Any


def upgrade_envelope(value: Any) -> dict[str, Any]:
    """Normalize any stored citations value into the envelope shape.

    - `None` → empty envelope.
    - `list` → legacy; wrap as `{items: list, sources: [], refs: []}`.
    - `dict` with the expected keys → pass through (defensive copy).
    - anything else → empty envelope.
    """
    if value is None:
        return {"items": [], "sources": [], "refs": []}
    if isinstance(value, list):
        return {"items": list(value), "sources": [], "refs": []}
    if isinstance(value, dict):
        return {
            "items": list(value.get("items") or []),
            "sources": list(value.get("sources") or []),
            "refs": list(value.get("refs") or []),
        }
    return {"items": [], "sources": [], "refs": []}


def as_legacy_items(value: Any) -> list[dict]:
    """Read a stored citations value as a flat list (back-compat for
    callers that haven't upgraded yet).
    """
    env = upgrade_envelope(value)
    return env["items"]
