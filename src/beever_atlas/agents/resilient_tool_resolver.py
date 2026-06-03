"""Soften ADK's hard-fail behaviour on unknown tool names.

Why
---
``google.adk.flows.llm_flows.functions._get_tool`` raises ``ValueError``
when an LLM calls a tool by a name that isn't in ``agent.tools``. The
exception terminates the entire agent stream — the operator sees
``Agent error during streaming`` and the user sees a wall of debug text.

Gemini models trained with the ADK tool ecosystem rarely hallucinate.
Other models — GLM, Llama, Qwen, smaller OpenAI models reached through
LiteLLM — sometimes invent tool names that look plausible
(``people-profile``, ``query-users``, …) even when prompted with the
canonical list. With ADK's default behaviour, one hallucination kills
the whole turn instead of giving the model a chance to retry.

Fix
---
ADK 2.1.0 routes the unknown-tool ``ValueError`` through an agent-level
``on_tool_error_callback``: it wraps ``_get_tool`` in ``try/except
ValueError``, builds a placeholder ``BaseTool`` whose ``.name`` is the
hallucinated name, and runs the registered error callbacks. If a
callback returns a dict, ADK injects it as the ``function_response`` fed
back to the LLM; if every callback returns ``None``, the original
``ValueError`` is re-raised (the fail-fast contract is preserved).

:func:`make_tool_error_callback` builds such a callback. The returned
dict carries the canonical tool list plus a ``did_you_mean`` suggestion;
the LLM sees "your tool name was wrong, here are the real names" and
retries on the same turn.

This replaces the previous process-global monkey-patch of ADK's private
``_get_tool``. The callback is per-agent, so every tool-dispatching
``LlmAgent`` must attach it explicitly (see
``beever_atlas.agents.query.qa_agent``).
"""

from __future__ import annotations

import difflib
import logging
from typing import Any, Callable

from google.adk.tools.base_tool import BaseTool
from google.adk.tools.tool_context import ToolContext

logger = logging.getLogger(__name__)


def _closest_tool_match(requested: str, available: list[str]) -> str | None:
    """Return the most likely real tool name when the LLM hallucinated a
    near-miss (e.g. ``list_channels`` → ``list_channels_tool``).

    Strategy:
      1. Exact / dash↔underscore swap — unambiguous, return immediately.
      2. Prefix/suffix-drop family (``foo`` ↔ ``foo_tool``) — unambiguous
         when the bare names match exactly under the suffix.
      3. Generic typo distance via ``difflib.get_close_matches`` (cutoff
         0.7) — handles underscore/dash drift and single-char typos.
      4. Substring containment (``search`` matching ``search_facts``)
         only as a LAST resort, because it has many ambiguous matches.
         When multiple substring candidates exist, pick the one with
         the shortest edit distance to the request rather than iteration
         order — that's the difference between guessing "search_facts"
         and "search_qa_history" when the LLM said just "search".
    """
    req = requested.lower()
    norm = req.replace("-", "_")

    # 1) Exact match (handles dash↔underscore swap too)
    for cand in available:
        c = cand.lower()
        if c == req or c == norm:
            return cand

    # 2) Suffix family — only when the BARE part matches exactly, so
    # ``list_channels`` resolves to ``list_channels_tool`` but ``search``
    # does NOT match ``search_facts`` here.
    for cand in available:
        c = cand.lower()
        if c.startswith(norm + "_") or norm.startswith(c + "_"):
            return cand

    # 3) Generic fuzzy typo match
    lower_available = [c.lower() for c in available]
    close = difflib.get_close_matches(norm, lower_available, n=1, cutoff=0.7)
    if close:
        for cand in available:
            if cand.lower() == close[0]:
                return cand

    # 4) Substring containment — last resort, pick BEST match by edit
    # distance instead of first-seen so an ambiguous ``search`` query
    # picks the closest candidate deterministically.
    substring_candidates = [
        cand for cand in available if (norm in cand.lower() or cand.lower() in norm)
    ]
    if substring_candidates:
        substring_candidates.sort(
            key=lambda cand: difflib.SequenceMatcher(None, norm, cand.lower()).ratio(),
            reverse=True,
        )
        return substring_candidates[0]

    return None


def build_unknown_tool_payload(requested_name: str, available_names: list[str]) -> dict[str, Any]:
    """Build the structured tool-result the LLM gets back when it names a
    tool that doesn't exist.

    The payload is JSON-serialisable and ADK injects it verbatim as the
    ``function_response``. Smaller open-source models (Gemma 2B/4B, Llama
    3.2 3B, …) often drop or add a name suffix when the agent registers
    15+ tools, so an explicit ``did_you_mean`` field lets weak models
    recover in one extra turn instead of giving up.
    """
    suggestion = _closest_tool_match(requested_name, available_names)
    logger.warning(
        "resilient_tool_resolver: model called unknown tool %r — "
        "returning soft error (did_you_mean=%r). Available: %s",
        requested_name,
        suggestion,
        ", ".join(available_names),
    )
    payload: dict[str, Any] = {
        "error": "tool_not_found",
        "requested_tool": requested_name,
        "available_tools": available_names,
    }
    if suggestion is not None:
        payload["did_you_mean"] = suggestion
        payload["hint"] = (
            f"The tool {requested_name!r} does not exist. The closest "
            f"available match is {suggestion!r} — retry the call with "
            "EXACTLY that name."
        )
    else:
        payload["hint"] = (
            f"The tool {requested_name!r} does not exist. Pick exactly "
            "one name from available_tools and retry. Tool names are "
            "case-sensitive."
        )
    return payload


def make_tool_error_callback(
    available_tool_names: list[str] | Callable[[], list[str]],
) -> Callable[[BaseTool, dict, ToolContext, Exception], dict | None]:
    """Build an agent-level ``on_tool_error_callback`` that turns ADK's
    unknown-tool ``ValueError`` into a structured tool-result the LLM can
    recover from on the same turn.

    Args:
        available_tool_names: the canonical tool names the agent exposes,
            either as a static list bound at construction or a callable
            returning the list lazily (e.g. when a SkillToolset resolves
            its tools at runtime). Names must match what ADK registers in
            ``tools_dict`` (which keys on ``tool.name``) for the
            ``did_you_mean`` suggestions to land.

    Returns:
        A callback with ADK's positional agent-level signature
        ``(tool, args, tool_context, error)``. It handles ONLY the
        unknown-tool case (``ValueError`` whose message contains "not
        found") and returns ``None`` for every other error so ADK
        re-raises it — the fail-fast contract is preserved for real tool
        failures.
    """

    # NOTE: the 2nd parameter MUST be named ``args`` — ADK invokes the
    # agent-level callback with ``args=`` as a keyword (the plugin-level
    # callback uses ``tool_args`` instead). See functions.py:507-513.
    def on_tool_error(
        tool: BaseTool,
        args: dict,  # noqa: ARG001 — required by ADK's keyword invocation
        tool_context: ToolContext,  # noqa: ARG001
        error: Exception,
    ) -> dict | None:
        # Only soften the hallucinated-tool case. Other errors (real tool
        # failures) propagate untouched so genuine bugs stay loud.
        if not (isinstance(error, ValueError) and "not found" in str(error)):
            return None
        # The placeholder BaseTool carries the hallucinated name as .name.
        requested_name = tool.name
        names = available_tool_names() if callable(available_tool_names) else available_tool_names
        return build_unknown_tool_payload(requested_name, sorted(names))

    return on_tool_error


__all__ = [
    "_closest_tool_match",
    "build_unknown_tool_payload",
    "make_tool_error_callback",
]
