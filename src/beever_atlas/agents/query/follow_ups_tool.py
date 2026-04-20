"""ADK tool: suggest_follow_ups.

Replaces the legacy prose-JSON FOLLOW_UPS regex antipattern with a
typed tool call. The agent is expected to invoke this at the end of its
turn with 1–3 contextual follow-up questions.

The tool stashes questions on a request-scoped collector (contextvar).
`_run_agent_stream` reads the collector after turn_complete and emits
the existing `follow_ups` SSE event from the typed list.

When `citation_registry_enabled=False`, the agent is not configured
with this tool, so the legacy regex path keeps running unchanged.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_MAX_QUESTIONS = 3
_MIN_QUESTION_LEN = 3


@dataclass
class FollowUpsCollector:
    """Holds the most recent suggestions for one turn."""

    questions: list[str] = field(default_factory=list)


_current_collector: ContextVar[FollowUpsCollector | None] = ContextVar(
    "follow_ups_collector", default=None
)


def current_collector() -> FollowUpsCollector | None:
    return _current_collector.get()


def bind_collector() -> tuple[FollowUpsCollector, Token]:
    collector = FollowUpsCollector()
    token = _current_collector.set(collector)
    return collector, token


def reset_collector(token: Token) -> None:
    _current_collector.reset(token)


def suggest_follow_ups(questions: list[str]) -> dict:
    """Call this tool to suggest 3 short follow-up questions as plain strings. No bullets, no markdown, no numbering prefixes. Each string is a complete question under 100 characters.

    Call this ONCE at the end of your answer. Do NOT write a FOLLOW_UPS
    JSON block in your prose — this tool replaces that mechanism.

    Args:
        questions: 1 to 3 short, natural-language follow-up questions
            the user might want to ask next. Each question should be a
            complete sentence ending with a question mark.

    Returns:
        A confirmation dict. The UI is updated from this tool call's
        arguments; the return value is only seen by the model.
    """
    cleaned = _clean(questions)
    collector = current_collector()
    if collector is not None:
        collector.questions = cleaned
    return {"ok": True, "count": len(cleaned)}


_BULLET_PREFIX_RE = re.compile(r"^[-*\d.\s]+")

# Same shape as stream_rewriter._LEFTOVER_TAG_RE. Duplicated here to avoid a
# circular import with the citation registry module; kept in lock-step.
_SRC_LITERAL_RE = re.compile(r"\[\s*(?:src:[^\[\]]*?|External:[^\[\]]*?)\]", re.IGNORECASE)


def _clean(questions: list[str]) -> list[str]:
    if not isinstance(questions, list):
        return []
    out: list[str] = []
    for q in questions:
        if not isinstance(q, str):
            continue
        # Scrub `[src:...]` / `[External:...]` literals that the LLM may
        # copy from tool-result citations into a follow-up question.
        scrubbed = _SRC_LITERAL_RE.sub("", q)
        stripped = _BULLET_PREFIX_RE.sub("", scrubbed.strip()).strip()
        # Collapse runs of whitespace left behind by the scrub.
        stripped = re.sub(r"\s{2,}", " ", stripped)
        if len(stripped) < _MIN_QUESTION_LEN:
            continue
        out.append(stripped)
        if len(out) >= _MAX_QUESTIONS:
            break
    return out
