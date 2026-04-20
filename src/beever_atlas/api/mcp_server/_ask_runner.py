"""ADK QA-runner wrapper for the MCP ``ask_channel`` tool.

This is the minimal non-SSE counterpart of the dashboard ``/api/ask`` endpoint
at ``src/beever_atlas/api/ask.py``. The dashboard path streams SSE events to a
React client; MCP clients want a single structured dict. We share the same
underlying ADK runner, QA agent, and session factory, so answers and tool-call
traces are consistent across surfaces — only the output envelope differs.

What we deliberately DO:

- Build the agent via :func:`beever_atlas.agents.query.qa_agent.get_agent_for_mode`
- Create a runner and a per-principal session
- Bind the principal contextvar for orchestration tools (same plumbing as
  ``api/ask.py``)
- Stream ADK events and accumulate: final assistant text, tool-call traces,
  citations extracted from the answer text
- Forward tool-call start/end as ``Context.info`` messages so MCP clients see
  progress in real time
- Enforce the 90s hard cap via ``asyncio.wait_for`` (defense in depth — the
  ``@mcp.tool(timeout=90)`` decorator covers the happy path)

What we deliberately DO NOT:

- Replicate the dashboard's chat-history loading, decomposition planner,
  per-channel policy lookup, or citation-registry rewrite. Those are dashboard
  UX concerns; MCP clients don't need them for v1.
- Persist the conversation to ``chat_history``. MCP sessions are ephemeral
  unless ``start_new_session`` is later extended to hook the same store.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field

from fastmcp import Context

logger = logging.getLogger(__name__)


_ASK_HARD_CAP_SECONDS = 90.0


_CITATION_PATTERN = re.compile(
    r"\[(\d+)\]\s+Author:\s*([^|]+)\|?\s*"
    r"(?:Channel:\s*([^|]+)\|?)?\s*"
    r"(?:Time:\s*([^\[\n]+))?"
)


@dataclass
class _AskResult:
    answer: str = ""
    citations: list[dict] = field(default_factory=list)
    follow_ups: list[str] = field(default_factory=list)
    tool_calls: list[dict] = field(default_factory=list)


def _extract_citations_from_text(text: str) -> list[dict]:
    """Parse the standard citation shape emitted by the QA agent.

    Mirrors the dashboard helper ``api/ask._extract_citations_from_text`` so
    MCP callers see the same citation shape the dashboard does.
    """
    citations: list[dict] = []
    for match in _CITATION_PATTERN.finditer(text or ""):
        citations.append(
            {
                "type": "channel_fact",
                "text": match.group(0).strip(),
                "number": match.group(1),
                "author": match.group(2).strip() if match.group(2) else "",
                "channel": match.group(3).strip() if match.group(3) else "",
                "timestamp": match.group(4).strip() if match.group(4) else "",
            }
        )
    return citations


async def run_ask_channel(
    principal_id: str,
    channel_id: str,
    question: str,
    mode: str,
    session_id: str | None,
    ctx: Context,
) -> dict:
    """Invoke the ADK QA runner for an MCP ``ask_channel`` call.

    Assumes :func:`beever_atlas.infra.channel_access.assert_channel_access`
    has ALREADY been called by the caller; this function just drives the
    runner and shapes the output.

    Returns a structured dict matching the ``ask_channel`` MCP spec:
    ``{answer, citations, follow_ups, metadata}``. On timeout, raises
    ``asyncio.TimeoutError`` (caller translates to ``answer_timeout``).
    """
    # Import lazily — ADK has heavy startup cost and we don't want to pay
    # it at module-import time for every FastMCP process that doesn't use
    # ask_channel.
    from google.adk.agents.run_config import RunConfig, StreamingMode
    from google.genai import types as genai_types

    from beever_atlas.agents.query.qa_agent import get_agent_for_mode
    from beever_atlas.agents.runner import create_runner, create_session
    from beever_atlas.agents.tools.orchestration_tools import (
        bind_principal,
        reset_principal,
    )

    # Session-id isolation: a client-supplied session_id is accepted only
    # when it is already namespaced under this principal. Otherwise a
    # principal could target another principal's session by guessing the
    # deterministic ``mcp:<hash>`` form. The default (no session_id supplied)
    # is ``mcp:<principal>:default`` — distinct from any custom id the caller
    # might try to reuse across principals.
    principal_namespace = f"mcp:{principal_id}"
    if session_id:
        if not session_id.startswith(principal_namespace):
            return {
                "error": "invalid_parameter",
                "parameter": "session_id",
                "detail": (
                    "session_id must be one returned by start_new_session or "
                    "an earlier ask_channel call; cross-principal reuse is "
                    "not permitted."
                ),
            }
        effective_session_id = session_id
    else:
        effective_session_id = f"{principal_namespace}:default"

    started_at = time.monotonic()

    agent = get_agent_for_mode(mode)
    runner = create_runner(agent)
    session = await create_session(user_id=principal_id, session_id=effective_session_id)

    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=f"[Channel: {channel_id}]\n\n{question}")],
    )

    result = _AskResult()
    principal_token = None

    try:
        principal_token = bind_principal(principal_id)
    except Exception:
        logger.warning(
            "event=mcp_ask_channel_bind_principal_failed principal=%s",
            principal_id,
            exc_info=True,
        )

    async def _drive_runner() -> None:
        stream = runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=new_message,
            run_config=RunConfig(streaming_mode=StreamingMode.SSE),
        )
        active_tool_starts: dict[str, float] = {}
        async for event in stream:
            if event.error_code or event.error_message:
                raise RuntimeError(event.error_message or f"ADK error: {event.error_code}")

            # Tool call starts — ADK emits FunctionCall parts.
            if not getattr(event, "partial", False):
                for fc in event.get_function_calls():
                    tool_name = fc.name or "unknown"
                    active_tool_starts[tool_name] = time.monotonic()
                    try:
                        await ctx.info(f"tool: {tool_name}")
                    except Exception:
                        pass

            # Tool call ends — FunctionResponse parts.
            for fr in event.get_function_responses():
                tool_name = fr.name or "unknown"
                start = active_tool_starts.pop(tool_name, time.monotonic())
                latency_ms = int((time.monotonic() - start) * 1000)
                result.tool_calls.append(
                    {
                        "tool_name": tool_name,
                        "latency_ms": latency_ms,
                    }
                )

            # Assistant text — only fold in the authoritative final event to
            # avoid concatenating partial streaming fragments (the final event
            # carries the assembled text).
            if not getattr(event, "partial", False):
                content = getattr(event, "content", None)
                if content and getattr(content, "parts", None):
                    for part in content.parts:
                        text = getattr(part, "text", None)
                        if text and getattr(content, "role", "") == "model":
                            result.answer += text

    try:
        await asyncio.wait_for(_drive_runner(), timeout=_ASK_HARD_CAP_SECONDS)
    finally:
        if principal_token is not None:
            try:
                reset_principal(principal_token)
            except Exception:
                logger.warning("failed to reset principal token in ask_channel", exc_info=True)

    # Citations come from parsing the assembled answer — the QA agent is
    # instructed to emit them in the `[N] Author: ... | Channel: ... | Time: ...`
    # shape that the dashboard also parses.
    result.citations = _extract_citations_from_text(result.answer)

    return {
        "answer": result.answer.strip(),
        "citations": result.citations,
        "follow_ups": result.follow_ups,
        "metadata": {
            "session_id": effective_session_id,
            "mode": mode,
            "duration_ms": int((time.monotonic() - started_at) * 1000),
            "tool_calls": result.tool_calls,
        },
    }


__all__ = ["run_ask_channel"]
