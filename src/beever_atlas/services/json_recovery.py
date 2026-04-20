"""Truncated JSON recovery utilities.

When an LLM response is cut off due to max_output_tokens, the JSON payload
is incomplete. This module attempts to salvage as many complete objects as
possible from the partial text rather than discarding the entire response.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class TruncationReport:
    """Metadata about a JSON recovery operation."""

    recovered_count: int
    """Number of complete top-level items recovered."""

    estimated_lost: int
    """Estimated number of items that could not be recovered."""

    raw_bytes: int
    """Length in bytes of the original (truncated) input."""

    last_boundary_offset: int
    """Byte offset of the last complete object boundary found."""


def recover_truncated_json_with_report(
    text: str,
) -> tuple[dict | list | None, TruncationReport]:
    """Like ``recover_truncated_json`` but also returns a ``TruncationReport``.

    Args:
        text: Raw text that may contain truncated JSON.

    Returns:
        A tuple ``(result, report)`` where *result* is the parsed value (or
        ``None``) and *report* carries recovery metadata.
    """
    raw_bytes = len(text.encode()) if text else 0
    stripped = text.strip() if text else ""

    # Fast path — already valid JSON; no truncation.
    if stripped:
        try:
            result = json.loads(stripped)
            count = len(result) if isinstance(result, (list, dict)) else 0
            report = TruncationReport(
                recovered_count=count,
                estimated_lost=0,
                raw_bytes=raw_bytes,
                last_boundary_offset=len(stripped),
            )
            return result, report
        except json.JSONDecodeError:
            pass

    boundary = _find_last_complete_boundary(stripped) if stripped else -1
    if boundary <= 0:
        report = TruncationReport(
            recovered_count=0,
            estimated_lost=0,
            raw_bytes=raw_bytes,
            last_boundary_offset=-1,
        )
        return None, report

    recovered_text = stripped[:boundary]
    closed = _close_open_structures(recovered_text)

    try:
        result = json.loads(closed)
        recovered_count = len(result) if isinstance(result, (list, dict)) else 0
        report = TruncationReport(
            recovered_count=recovered_count,
            estimated_lost=1,  # at minimum one object was cut off
            raw_bytes=raw_bytes,
            last_boundary_offset=boundary,
        )
        return result, report
    except json.JSONDecodeError:
        report = TruncationReport(
            recovered_count=0,
            estimated_lost=1,
            raw_bytes=raw_bytes,
            last_boundary_offset=boundary,
        )
        return None, report


def recover_truncated_json(text: str) -> dict | list | None:
    """Attempt to parse JSON, recovering partial data if the text is truncated.

    Algorithm:
    1. Try a straight ``json.loads`` — return immediately if it succeeds.
    2. Search backwards for the last complete object boundary (``},`` or ``}]``).
    3. Truncate at that boundary and close any open arrays/objects.
    4. Try ``json.loads`` on the repaired text.
    5. Return ``None`` if recovery fails.

    Args:
        text: Raw text that may contain truncated JSON.

    Returns:
        Parsed JSON value (dict or list), or ``None`` on failure.
    """
    if not text or not text.strip():
        return None

    stripped = text.strip()

    # Fast path — text is already valid JSON.
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Find the last complete object boundary.
    boundary = _find_last_complete_boundary(stripped)
    if boundary <= 0:
        logger.debug("json_recovery: no object boundary found, cannot recover")
        return None
    recovered = stripped[:boundary]

    # Close any unmatched opening brackets/braces.
    closed = _close_open_structures(recovered)

    try:
        result = json.loads(closed)
        logger.debug("json_recovery: successfully recovered truncated JSON")
        return result
    except json.JSONDecodeError as exc:
        logger.debug("json_recovery: recovery attempt failed: %s", exc)
        return None


def recover_facts_from_truncated(text: str) -> dict | None:
    """Recover a ``{"facts": [...]}`` response that may be truncated.

    Parses as much of the ``facts`` list as possible and returns a dict
    with the complete fact objects that were found.

    Args:
        text: Raw LLM output, possibly truncated mid-JSON.

    Returns:
        ``{"facts": [<complete fact objects>]}`` or ``None`` if nothing
        could be recovered.
    """
    result, report = recover_truncated_json_with_report(text)
    if result is None:
        logger.warning("json_recovery: could not recover any facts from truncated JSON")
        return None

    if not isinstance(result, dict):
        logger.warning("json_recovery: expected dict at top level, got %s", type(result).__name__)
        return None

    facts = result.get("facts", [])
    if not isinstance(facts, list):
        facts = []

    count = len(facts)
    estimated_lost = max(report.estimated_lost, 1 if report.last_boundary_offset > 0 else 0)
    if count > 0 or report.estimated_lost > 0:
        logger.warning(
            "json_recovery: truncated extract batch=facts recovered=%d lost_estimate=%d "
            "raw_bytes=%d last_boundary_offset=%d",
            count,
            estimated_lost,
            report.raw_bytes,
            report.last_boundary_offset,
        )

    return {"facts": facts}


def recover_entities_from_truncated(text: str) -> dict | None:
    """Recover a ``{"entities": [...], "relationships": [...]}`` response.

    Parses as much of the ``entities`` and ``relationships`` lists as
    possible from a potentially truncated response.

    Args:
        text: Raw LLM output, possibly truncated mid-JSON.

    Returns:
        ``{"entities": [...], "relationships": [...]}`` with whatever
        complete objects were found, or ``None`` if nothing could be
        recovered.
    """
    result, report = recover_truncated_json_with_report(text)
    if result is None:
        logger.warning("json_recovery: could not recover any entities from truncated JSON")
        return None

    if not isinstance(result, dict):
        logger.warning("json_recovery: expected dict at top level, got %s", type(result).__name__)
        return None

    entities = result.get("entities", [])
    relationships = result.get("relationships", [])
    if not isinstance(entities, list):
        entities = []
    if not isinstance(relationships, list):
        relationships = []

    total = len(entities) + len(relationships)
    estimated_lost = max(report.estimated_lost, 1 if report.last_boundary_offset > 0 else 0)
    if total > 0 or report.estimated_lost > 0:
        logger.warning(
            "json_recovery: truncated extract batch=entities recovered=%d lost_estimate=%d "
            "raw_bytes=%d last_boundary_offset=%d",
            total,
            estimated_lost,
            report.raw_bytes,
            report.last_boundary_offset,
        )

    return {"entities": entities, "relationships": relationships}


def recover_validation_from_truncated(text: str) -> dict | None:
    """Recover a ``ValidationResult``-shaped response that may be truncated.

    Expects ``{"entities": [...], "relationships": [...], "merges": [...]}``.
    Returns a dict with whatever complete objects were found, or ``None``.

    Args:
        text: Raw LLM output, possibly truncated mid-JSON.

    Returns:
        A dict with ``entities``, ``relationships``, and ``merges`` keys,
        or ``None`` if nothing could be recovered.
    """
    result, report = recover_truncated_json_with_report(text)
    if result is None:
        logger.warning("json_recovery: could not recover any validation result from truncated JSON")
        return None

    if not isinstance(result, dict):
        logger.warning("json_recovery: expected dict at top level, got %s", type(result).__name__)
        return None

    entities = result.get("entities", [])
    relationships = result.get("relationships", [])
    merges = result.get("merges", [])
    if not isinstance(entities, list):
        entities = []
    if not isinstance(relationships, list):
        relationships = []
    if not isinstance(merges, list):
        merges = []

    total = len(entities) + len(relationships) + len(merges)
    estimated_lost = max(report.estimated_lost, 1 if report.last_boundary_offset > 0 else 0)
    if total > 0 or report.estimated_lost > 0:
        logger.warning(
            "json_recovery: truncated validation result recovered=%d lost_estimate=%d "
            "raw_bytes=%d last_boundary_offset=%d",
            total,
            estimated_lost,
            report.raw_bytes,
            report.last_boundary_offset,
        )

    return {"entities": entities, "relationships": relationships, "merges": merges}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _find_last_complete_boundary(text: str) -> int:
    """Return an index at which ``text`` can be safely truncated.

    Walks forward tracking JSON string context so that ``}``, ``,`` and
    ``]`` characters *inside* string literals cannot be misidentified as
    structural boundaries. The returned index points just after the last
    ``}`` that is followed by ``,`` or ``]`` in structural position — i.e.
    the end of the most recent complete object inside an enclosing array.

    Returns ``-1`` when no complete boundary has been observed.
    """
    in_string = False
    escape = False
    last_brace_close = -1  # index just after a structural '}'
    last_safe = -1
    for i, c in enumerate(text):
        if escape:
            escape = False
            continue
        if in_string:
            if c == "\\":
                escape = True
            elif c == '"':
                in_string = False
            continue
        if c == '"':
            in_string = True
            last_brace_close = -1
            continue
        if c == "}":
            last_brace_close = i + 1
            continue
        if c == "]":
            # A structural ``]`` after a complete object closes the array;
            # keep the ``]`` so arrays close naturally.
            if last_brace_close != -1:
                last_safe = i + 1
            last_brace_close = -1
            continue
        if c == ",":
            # Drop the comma — caller will close remaining structures.
            if last_brace_close != -1:
                last_safe = last_brace_close
            last_brace_close = -1
            continue
        if not c.isspace():
            last_brace_close = -1
    return last_safe


def _close_open_structures(text: str) -> str:
    """Append the minimum closing brackets/braces to make ``text`` valid JSON.

    Counts unmatched ``[`` and ``{`` (ignoring characters inside strings) and
    appends the corresponding ``]`` / ``}`` tokens in reverse order.
    """
    closers: list[str] = []
    in_string = False
    escape_next = False

    for ch in text:
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            closers.append("}")
        elif ch == "[":
            closers.append("]")
        elif ch in ("}", "]"):
            if closers and closers[-1] == ch:
                closers.pop()

    return text + "".join(reversed(closers))
