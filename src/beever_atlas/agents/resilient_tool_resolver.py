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

import logging
from typing import Any

from google.adk.tools.base_tool import BaseTool

logger = logging.getLogger(__name__)


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
        logger.warning(
            "resilient_tool_resolver: model called unknown tool %r — "
            "returning soft error. Available: %s",
            self._requested,
            ", ".join(self._available),
        )
        return {
            "error": "tool_not_found",
            "requested_tool": self._requested,
            "available_tools": self._available,
            "hint": (
                f"The tool {self._requested!r} does not exist. Pick exactly one "
                f"name from available_tools and retry. Tool names are case-sensitive."
            ),
        }


def install_resilient_tool_resolver() -> None:
    """Monkey-patch ADK's ``_get_tool`` to return a stub on unknown names.

    Called once at server boot. Safe to call again — the patch tags the
    function with a marker attribute so re-installation is a no-op.
    """
    from google.adk.flows.llm_flows import functions as adk_functions

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
