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
Install a stub tool that ADK can dispatch through its normal flow. The
stub's ``run_async`` returns a structured error containing the
canonical tool list. ADK feeds that back to the LLM as a tool-result
message; the LLM sees "your tool name was wrong, here are the real
names" and tries again on the same turn.

The default ADK behaviour stays opt-out — pass ``enabled=False`` (e.g.
to investigate a genuine tool-registration bug) and the original
fail-fast contract returns.

Idempotent — re-installing in tests / hot reload is safe.
"""

from __future__ import annotations

import difflib
import logging
from typing import Any

from google.adk.tools.base_tool import BaseTool

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


class _UnknownToolStub(BaseTool):
    """Echoes back a tool-error response when the LLM names a tool that
    doesn't exist. Visible to the LLM as a normal tool-result, so it can
    try again with one of the listed valid names on the same turn."""

    def __init__(self, requested_name: str, available_names: list[str]) -> None:
        super().__init__(
            name=requested_name,
            description=(
                f"Stub for the hallucinated tool name {requested_name!r}. "
                "Returns a structured error so the LLM can retry with a valid "
                "tool name."
            ),
        )
        self._requested = requested_name
        self._available = available_names

    async def run_async(self, *, args: dict[str, Any], tool_context: Any) -> Any:  # noqa: ARG002
        suggestion = _closest_tool_match(self._requested, self._available)
        logger.warning(
            "resilient_tool_resolver: model called unknown tool %r — "
            "returning soft error (did_you_mean=%r). Available: %s",
            self._requested,
            suggestion,
            ", ".join(self._available),
        )
        # Smaller open-source models (Gemma 2B/4B, Llama 3.2 3B, …) often
        # drop or add a name suffix when the agent registers 15+ tools.
        # Including an explicit ``did_you_mean`` field lets weak models
        # recover in one extra turn instead of giving up.
        payload: dict[str, Any] = {
            "error": "tool_not_found",
            "requested_tool": self._requested,
            "available_tools": self._available,
        }
        if suggestion is not None:
            payload["did_you_mean"] = suggestion
            payload["hint"] = (
                f"The tool {self._requested!r} does not exist. The closest "
                f"available match is {suggestion!r} — retry the call with "
                "EXACTLY that name."
            )
        else:
            payload["hint"] = (
                f"The tool {self._requested!r} does not exist. Pick exactly "
                "one name from available_tools and retry. Tool names are "
                "case-sensitive."
            )
        return payload


def install_resilient_tool_resolver() -> None:
    """Monkey-patch ADK's ``_get_tool`` to return a stub on unknown names.

    Called once at server boot. Safe to call again — the patch tags the
    function with a marker attribute so re-installation is a no-op.

    Defensive: ``_get_tool`` is a private ADK symbol that could be
    renamed or moved by a future ADK release. When it's missing, log a
    clear warning and return — the operator will see the warning at
    boot and can pin a known-good ADK version. The agent stream still
    runs unpatched; behaviour reverts to ADK's hard ``ValueError`` on
    unknown tool names (the original ADK contract).
    """
    from google.adk.flows.llm_flows import functions as adk_functions

    if not hasattr(adk_functions, "_get_tool"):
        logger.warning(
            "resilient_tool_resolver: ADK's flows.llm_flows.functions._get_tool "
            "is missing — likely an ADK version upgrade renamed it. Falling back "
            "to the unpatched contract (hard ValueError on hallucinated tool "
            "names). Pin the working ADK range in pyproject.toml or update this "
            "patch."
        )
        return

    if getattr(adk_functions._get_tool, "_beever_resilient", False):
        return

    def _resilient_get_tool(function_call: Any, tools_dict: dict[str, BaseTool]) -> BaseTool:
        name = getattr(function_call, "name", None)
        if name in tools_dict:
            return tools_dict[name]
        # Don't raise — return a stub that emits a tool-result back to the
        # model with the canonical tool list, letting it recover on the
        # same turn instead of killing the stream.
        return _UnknownToolStub(
            requested_name=str(name) if name is not None else "<unknown>",
            available_names=sorted(tools_dict.keys()),
        )

    _resilient_get_tool._beever_resilient = True  # type: ignore[attr-defined]
    adk_functions._get_tool = _resilient_get_tool
    logger.info(
        "resilient_tool_resolver: installed — unknown tool names now return "
        "a soft error instead of crashing the agent stream"
    )


__all__ = ["install_resilient_tool_resolver", "_UnknownToolStub"]
