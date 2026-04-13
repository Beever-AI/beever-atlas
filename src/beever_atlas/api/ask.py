"""SSE streaming Q&A endpoint using ADK Runner with tool call event emission."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, AsyncGenerator

if TYPE_CHECKING:
    from beever_atlas.agents.query.decomposer import QueryPlan

from fastapi import APIRouter, HTTPException, Request, UploadFile, File as FastAPIFile
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types as genai_types

from beever_atlas.agents.runner import create_runner, create_session

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB
SUPPORTED_MIME_TYPES = {
    "application/pdf", "image/png", "image/jpeg",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain", "text/csv",
}


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="The question to ask")
    include_citations: bool = Field(default=True)
    max_results: int = Field(default=10, ge=1, le=50)
    session_id: str | None = Field(default=None, description="Resume an existing session")
    mode: str = Field(default="deep", pattern="^(quick|deep|summarize)$")
    attachments: list[dict] = Field(default_factory=list, description="Attached file content")
    disabled_tools: list[str] = Field(
        default_factory=list,
        description="Per-request tool names to disable. Unknown names are ignored with a warning.",
    )


class FeedbackRequest(BaseModel):
    session_id: str
    message_id: str
    rating: str = Field(..., pattern="^(up|down)$")
    comment: str | None = None


def _sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


def _extract_user_id(request: Request) -> str:
    """Extract user_id from request auth state.

    Checks request.state.user_id (set by auth middleware) and falls back
    to "api_user" for unauthenticated/development requests.
    """
    return getattr(request.state, "user_id", None) or "api_user"


def _extract_citations_from_text(text: str) -> list[dict]:
    """Extract citation-format lines from agent response text.

    Looks for lines like: [1] Author: @handle | Channel: #name | Time: ts
    Returns list of citation dicts for the SSE citations event.
    """
    citations = []
    for match in re.finditer(
        r"\[(\d+)\]\s+Author:\s*([^|]+)\|?\s*(?:Channel:\s*([^|]+)\|?)?\s*(?:Time:\s*([^\[]+))?",
        text,
    ):
        citations.append({
            "type": "channel_fact",
            "text": match.group(0).strip(),
            "number": match.group(1),
            "author": match.group(2).strip() if match.group(2) else "",
            "channel": match.group(3).strip() if match.group(3) else "",
            "timestamp": match.group(4).strip() if match.group(4) else "",
        })
    return citations


async def _build_decomposed_prompt(
    question: str, channel_id: str
) -> "tuple[str, QueryPlan | None]":
    """Run QueryDecomposer and annotate the prompt for complex questions.

    Returns a tuple of (prompt_text, plan_or_None).  plan is None when the
    question is simple (no decomposition event should be emitted).
    """
    from beever_atlas.agents.query.decomposer import decompose

    plan = await decompose(question)
    logger.info(
        "QueryDecomposer result: is_simple=%s internal=%d external=%d for %r",
        plan.is_simple, len(plan.internal_queries), len(plan.external_queries), question[:80],
    )
    if plan.is_simple:
        return f"[Channel: {channel_id}]\n\n{question}", None

    # For complex questions, hint the agent about sub-queries so it can
    # plan tool calls more efficiently.
    sub_q_lines = "\n".join(
        f"  - [{sq.focus}] {sq.query}" for sq in plan.internal_queries
    )
    ext_lines = (
        "\n".join(f"  - [{sq.focus}] {sq.query}" for sq in plan.external_queries)
        if plan.external_queries
        else "  (none)"
    )
    prompt = (
        f"[Channel: {channel_id}]\n\n"
        f"{question}\n\n"
        f"<decomposition>\n"
        f"Internal sub-queries (search these in parallel):\n{sub_q_lines}\n"
        f"External sub-queries:\n{ext_lines}\n"
        f"</decomposition>"
    )
    return prompt, plan


async def _load_chat_history_parts(session_id: str) -> list[genai_types.Content]:
    """Load last 10 turns from ChatHistoryStore as genai Content objects."""
    try:
        from beever_atlas.infra.config import get_settings
        from beever_atlas.stores.chat_history_store import ChatHistoryStore

        settings = get_settings()
        store = ChatHistoryStore(settings.mongodb_uri)
        await store.startup()
        try:
            messages = await store.get_context_messages(session_id=session_id)
        finally:
            store.close()

        contents = []
        for msg in messages:
            role = "user" if msg.get("role") == "user" else "model"
            contents.append(
                genai_types.Content(
                    role=role,
                    parts=[genai_types.Part(text=msg.get("content", ""))],
                )
            )
        return contents
    except Exception:
        logger.debug("Could not load chat history for session=%s", session_id)
        return []


async def _run_agent_stream(
    question: str,
    channel_id: str,
    session_id: str,
    user_id: str,
    request: Request,
    mode: str = "deep",
    attachments: list[dict] | None = None,
    use_v2_schema: bool = False,
    disabled_tools: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """Run the ADK agent and yield SSE events including tool call progress."""
    from beever_atlas.agents.query.qa_agent import (
        create_qa_agent,
        get_agent_for_mode,
        _tool_name,
    )
    from beever_atlas.agents.tools import QA_TOOLS, QA_TOOL_DESCRIPTORS

    disabled_tools = disabled_tools or []
    if disabled_tools:
        known_names = {d["name"] for d in QA_TOOL_DESCRIPTORS}
        effective_disabled: list[str] = []
        for name in disabled_tools:
            if name in known_names:
                effective_disabled.append(name)
            else:
                logger.warning(
                    "Ignoring unknown tool name in disabled_tools: %r", name
                )
        if effective_disabled:
            # Build a NEW list — never mutate QA_TOOLS.
            filtered = [t for t in QA_TOOLS if _tool_name(t) not in effective_disabled]
            refusal_clause = (
                "\n\nThe following tools are disabled for this request: "
                f"{', '.join(effective_disabled)}. If answering the question "
                "requires any of them, politely refuse and name the disabled tool(s)."
            )
            agent = create_qa_agent(
                mode=mode, tools=filtered, extra_instruction=refusal_clause
            )
        else:
            agent = get_agent_for_mode(mode)
    else:
        agent = get_agent_for_mode(mode)
    runner = create_runner(agent)
    session = await create_session(user_id=user_id)

    # ----- Settings flags ---------------------------------------------
    from beever_atlas.infra.config import get_settings
    _settings = get_settings()
    sse_streaming = bool(getattr(_settings, "qa_adk_streaming_sse", False))

    # ----- Citation registry (Phase 1, flag-gated) --------------------
    _registry_enabled = bool(getattr(_settings, "citation_registry_enabled", False))

    _registry = None
    _registry_token = None
    _follow_ups_collector = None
    _follow_ups_token = None
    _rewriter = None
    if _registry_enabled:
        from beever_atlas.agents.citations import registry as _citation_registry_mod
        from beever_atlas.agents.citations.permalink_resolver import default_resolver
        from beever_atlas.agents.query.follow_ups_tool import bind_collector
        from beever_atlas.agents.query.stream_rewriter import StreamRewriter

        _registry, _registry_token = _citation_registry_mod.bind(session_id=session_id)
        _registry.set_permalink_resolver(default_resolver)
        _follow_ups_collector, _follow_ups_token = bind_collector()
        _rewriter = StreamRewriter(_registry)

    # Task 4.8: Load prior conversation turns so agent has continuity
    history_parts = await _load_chat_history_parts(session_id)

    # Task 4.3: Decompose question and annotate prompt for complex questions
    prompt_text, _decomposition_plan = await _build_decomposed_prompt(question, channel_id)

    # Inject attachment content
    if attachments:
        attachment_sections = []
        for att in attachments:
            attachment_sections.append(
                f"## Attached file: {att.get('filename', 'unknown')}\n{att.get('extracted_text', '')}"
            )
        prompt_text += "\n\n" + "\n\n".join(attachment_sections)

    # Inject history context as a text prefix when prior turns exist
    if history_parts:
        history_lines = []
        for h in history_parts:
            role_label = "User" if h.role == "user" else "Assistant"
            text = h.parts[0].text if h.parts else ""
            history_lines.append(f"[{role_label}]: {text}")
        history_ctx = "\n".join(history_lines)
        prompt_text = (
            f"<prior_conversation>\n{history_ctx}\n</prior_conversation>\n\n"
            + prompt_text
        )

    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=prompt_text)],
    )

    accumulated_text = ""
    accumulated_thinking = ""
    # Persisted trace of tool calls in the order they appeared. Entries are
    # upgraded in place when the matching FunctionResponse arrives.
    persisted_tool_calls: list[dict] = []
    # Track active tool calls for latency measurement: tool_name → start_time
    active_tool_calls: dict[str, float] = {}
    done_sent = False
    # Thinking state tracking
    thinking_start: float | None = None
    thinking_ended = False
    thinking_duration_ms: int | None = None

    # Emit decomposition event before the agent starts, when the question was
    # complex enough to warrant sub-query planning.
    if _decomposition_plan is not None:
        yield _sse_event("decomposition", {
            "internal": [
                {"label": sq.focus, "query": sq.query}
                for sq in _decomposition_plan.internal_queries
            ],
            "external": [
                {"label": sq.focus, "query": sq.query}
                for sq in _decomposition_plan.external_queries
            ],
        })

    try:
        if sse_streaming:
            _stream = runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=new_message,
                run_config=RunConfig(streaming_mode=StreamingMode.SSE),
            )
        else:
            _stream = runner.run_async(
                user_id=session.user_id,
                session_id=session.id,
                new_message=new_message,
            )
        async for event in _stream:
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping agent stream")
                break

            if event.error_code or event.error_message:
                yield _sse_event("error", {
                    "message": event.error_message or "Unknown error",
                    "code": event.error_code or "AGENT_ERROR",
                })
                done_sent = True
                return

            # Tool call start — ADK emits FunctionCall parts before tool executes.
            # With SSE streaming, partial events carry incomplete JSON args; skip
            # them and only fire tool_call_start on the fully-assembled final event.
            if not getattr(event, "partial", False):
                for fc in event.get_function_calls():
                    tool_name = fc.name or "unknown"
                    tool_input = fc.args or {}
                    active_tool_calls[tool_name] = time.monotonic()
                    normalized_input = tool_input if isinstance(tool_input, dict) else {}
                    persisted_tool_calls.append({
                        "tool_name": tool_name,
                        "input": normalized_input,
                        "status": "running",
                    })
                    yield _sse_event("tool_call_start", {
                        "tool_name": tool_name,
                        "input": normalized_input,
                    })

            # Tool call end — ADK emits FunctionResponse parts after tool returns
            for fr in event.get_function_responses():
                tool_name = fr.name or "unknown"
                start_time = active_tool_calls.pop(tool_name, time.monotonic())
                latency_ms = int((time.monotonic() - start_time) * 1000)
                result = fr.response or {}
                # Estimate facts_found from result size
                facts_found = 0
                if isinstance(result, list):
                    facts_found = len(result)
                elif isinstance(result, dict):
                    facts_found = 1
                elif isinstance(result, str):
                    facts_found = result.count('"text"')
                result_summary = str(result)[:100] if result else ""

                # Upgrade the matching persisted entry (latest running entry
                # with this tool_name) with the finalized result.
                for entry in reversed(persisted_tool_calls):
                    if entry["tool_name"] == tool_name and entry.get("status") == "running":
                        entry["status"] = "done"
                        entry["result_summary"] = result_summary
                        entry["latency_ms"] = latency_ms
                        entry["facts_found"] = facts_found
                        break

                yield _sse_event("tool_call_end", {
                    "tool_name": tool_name,
                    "result_summary": result_summary,
                    "latency_ms": latency_ms,
                    "facts_found": facts_found,
                })

            # Text content streaming (with thinking detection)
            # INVARIANT: parts with thought=True MUST only yield "thinking" events,
            # never "response_delta". Do not relax this check without reviewing
            # downstream citation and history persistence logic.
            #
            # SSE streaming mode gate:
            # - Flag ON + partial=True  → emit response_delta/thinking, accumulate.
            # - Flag ON + partial=False → skip emission (final aggregate); fall through
            #   to turn_complete bookkeeping only. No double-emission.
            # - Flag OFF               → original behavior, byte-identical.
            _event_is_partial = getattr(event, "partial", False)
            _skip_text_emit = sse_streaming and not _event_is_partial

            if event.content and event.content.parts:
                for part in event.content.parts:
                    part_is_thought = getattr(part, "thought", False)
                    if part_is_thought:
                        # Thinking token from Gemini via BuiltInPlanner — emit
                        # only the "thinking" SSE event; never response_delta.
                        if part.text:
                            if thinking_start is None:
                                thinking_start = time.monotonic()
                            accumulated_thinking += part.text
                            if not _skip_text_emit:
                                yield _sse_event("thinking", {"text": part.text})
                    elif part.text:
                        # Belt-and-suspenders: if thought flag somehow leaks here, drop it.
                        if getattr(part, "thought", False):
                            logger.warning(
                                "Dropping part with thought=True from response_delta path "
                                "(session=%s)",
                                session_id,
                            )
                            continue
                        if _skip_text_emit:
                            # Flag ON, final aggregate event — skip SSE emission to
                            # avoid double-sending text already streamed via partials.
                            # BUT: if no partials contributed text (e.g. tool-only
                            # turn, very short answer, or ADK emitted a single
                            # partial=False event), the final aggregate is our only
                            # source. Accumulate it so persistence gets the answer.
                            if not accumulated_text:
                                if _rewriter is not None:
                                    rewritten = _rewriter.feed(part.text)
                                    if rewritten:
                                        accumulated_text += rewritten
                                else:
                                    accumulated_text += part.text
                            continue
                        # Regular response text — emit thinking_done if transitioning
                        if thinking_start is not None and not thinking_ended:
                            thinking_ended = True
                            thinking_duration_ms = int((time.monotonic() - thinking_start) * 1000)
                            yield _sse_event("thinking_done", {"duration_ms": thinking_duration_ms})
                        # When the registry is active, rewrite [src:xxx] tags
                        # to [N] before the chunk hits the wire. Flag-off path
                        # emits part.text unchanged (legacy behavior).
                        if _rewriter is not None:
                            rewritten = _rewriter.feed(part.text)
                            if rewritten:
                                yield _sse_event("response_delta", {"delta": rewritten})
                                accumulated_text += rewritten
                        else:
                            yield _sse_event("response_delta", {"delta": part.text})
                            accumulated_text += part.text

            # Turn complete
            if event.turn_complete:
                # Flush any buffered text in the rewriter (mid-tag remainders).
                if _rewriter is not None:
                    tail = _rewriter.flush()
                    if tail:
                        yield _sse_event("response_delta", {"delta": tail})
                        accumulated_text += tail

                # Build citations: registry-backed envelope (flag on) or
                # legacy regex-parsed list (flag off).
                if _registry is not None:
                    envelope = _registry.finalize(accumulated_text)
                    citations_payload = envelope.to_dict()
                    citations = envelope.items  # for persistence back-compat
                    logger.info(
                        "citation_registry turn summary: "
                        "session=%s registered=%d referenced=%d permalink_nulls=%s",
                        session_id,
                        _registry.registered_count,
                        _registry.referenced_count,
                        _registry.permalink_null_by_kind(),
                    )
                    yield _sse_event("citations", citations_payload)
                else:
                    citations = _extract_citations_from_text(accumulated_text)
                    yield _sse_event("citations", {"items": citations})

                # Follow-ups: prefer the tool-collector (Phase 1 path). If
                # the LLM didn't call the tool (or the tool isn't registered
                # on the agent yet), fall back to the legacy prose regex so
                # we never lose follow-ups during rollout.
                follow_ups: list[str] = []
                if _follow_ups_collector is not None and _follow_ups_collector.questions:
                    follow_ups = list(_follow_ups_collector.questions)
                if not follow_ups:
                    follow_up_match = re.search(
                        r'FOLLOW_UPS:\s*\[([^\]]*)\]', accumulated_text
                    )
                    if follow_up_match:
                        try:
                            follow_ups = json.loads(f'[{follow_up_match.group(1)}]')
                            accumulated_text = re.sub(
                                r'\n*---\n*FOLLOW_UPS:\s*\[.*?\]', '', accumulated_text
                            ).rstrip()
                        except (json.JSONDecodeError, ValueError):
                            pass

                if follow_ups:
                    yield _sse_event("follow_ups", {"suggestions": follow_ups})

                # Onboarding length monitor (warn-only, no truncation).
                try:
                    from beever_atlas.infra.config import get_settings
                    _monitor_on = get_settings().qa_onboarding_length_monitor
                except Exception:
                    _monitor_on = True
                if mode != "deep" and _monitor_on and len(accumulated_text) > 1500:
                    logger.warning(
                        "onboarding response exceeded 1500 chars: %d", len(accumulated_text)
                    )

                yield _sse_event("metadata", {
                    "route": "qa_agent",
                    "confidence": 0.85,
                    "cost_usd": 0.0,
                    "channel_id": channel_id,
                    "session_id": session_id,
                    "mode": mode,
                })
                await _persist_qa_history(
                    question=question,
                    answer=accumulated_text,
                    citations=citations,
                    channel_id=channel_id,
                    user_id=user_id,
                    session_id=session_id,
                    use_v2_schema=use_v2_schema,
                    thinking_text=accumulated_thinking,
                    thinking_duration_ms=thinking_duration_ms,
                    tool_calls=persisted_tool_calls,
                )
                yield _sse_event("done", {})
                done_sent = True
                return

    except asyncio.CancelledError:
        logger.info("Agent stream cancelled")
        yield _sse_event("error", {
            "message": "Request cancelled",
            "code": "CANCELLED",
        })
        done_sent = True
    except Exception as e:
        logger.exception("Agent error during streaming")
        yield _sse_event("error", {
            "message": str(e),
            "code": "AGENT_ERROR",
        })
        done_sent = True
    finally:
        # Reset citation registry contextvars regardless of how we got here.
        if _registry_token is not None:
            try:
                from beever_atlas.agents.citations import registry as _citation_registry_mod
                _citation_registry_mod.reset(_registry_token)
            except Exception:
                logger.warning("failed to reset citation registry token", exc_info=True)
        if _follow_ups_token is not None:
            try:
                from beever_atlas.agents.query.follow_ups_tool import reset_collector
                reset_collector(_follow_ups_token)
            except Exception:
                logger.warning("failed to reset follow_ups collector", exc_info=True)

        if not done_sent:
            # In SSE streaming mode (StreamingMode.SSE), ADK may deliver the
            # final text via partial=True events and emit a terminal
            # partial=False aggregate that carries no turn_complete flag.
            # The safety-net below handles this correctly; downgraded from
            # WARNING to INFO because it fires on every normal SSE completion
            # and is not indicative of an error.
            logger.info(
                "Agent stream ended without turn_complete for channel=%s; "
                "sending done event as safety net",
                channel_id,
            )
            # Persist even when turn_complete didn't fire (e.g., thinking planner flow)
            if accumulated_text.strip():
                # Flush rewriter + emit envelope when registry is active.
                if _rewriter is not None and _registry is not None:
                    tail = _rewriter.flush()
                    if tail:
                        yield _sse_event("response_delta", {"delta": tail})
                        accumulated_text += tail
                    envelope = _registry.finalize(accumulated_text)
                    citations = envelope.items
                    yield _sse_event("citations", envelope.to_dict())
                else:
                    citations = _extract_citations_from_text(accumulated_text)
                    yield _sse_event("citations", {"items": citations})

                # Extract follow-ups — prefer the tool-collector (Phase 1)
                # and fall back to the legacy prose regex.
                follow_ups: list[str] = []
                if _follow_ups_collector is not None and _follow_ups_collector.questions:
                    follow_ups = list(_follow_ups_collector.questions)
                if not follow_ups:
                    follow_up_match = re.search(
                        r'FOLLOW_UPS:\s*\[([^\]]*)\]', accumulated_text
                    )
                    if follow_up_match:
                        try:
                            follow_ups = json.loads(f'[{follow_up_match.group(1)}]')
                            accumulated_text = re.sub(
                                r'\n*---\n*FOLLOW_UPS:\s*\[.*?\]', '', accumulated_text
                            ).rstrip()
                        except (json.JSONDecodeError, ValueError):
                            pass
                if follow_ups:
                    yield _sse_event("follow_ups", {"suggestions": follow_ups})

                yield _sse_event("metadata", {
                    "route": "qa_agent",
                    "confidence": 0.85,
                    "cost_usd": 0.0,
                    "channel_id": channel_id,
                    "session_id": session_id,
                    "mode": mode,
                })
                await _persist_qa_history(
                    question=question,
                    answer=accumulated_text,
                    citations=citations,
                    channel_id=channel_id,
                    user_id=user_id,
                    session_id=session_id,
                    use_v2_schema=use_v2_schema,
                    thinking_text=accumulated_thinking,
                    thinking_duration_ms=thinking_duration_ms,
                    tool_calls=persisted_tool_calls,
                )
            yield _sse_event("done", {})


THINKING_MAX_BYTES = 20 * 1024  # Cap persisted reasoning text per message


def _build_thinking_doc(
    thinking_text: str, thinking_duration_ms: int | None
) -> dict | None:
    """Return the persisted thinking subdoc, or None if nothing to save.

    Truncates raw reasoning to THINKING_MAX_BYTES so a single message can't
    blow past MongoDB's 16MB document limit on verbose multi-turn sessions.
    """
    if not thinking_text and thinking_duration_ms is None:
        return None
    encoded = thinking_text.encode("utf-8")
    truncated = len(encoded) > THINKING_MAX_BYTES
    if truncated:
        thinking_text = encoded[:THINKING_MAX_BYTES].decode("utf-8", errors="ignore")
    return {
        "text": thinking_text,
        "duration_ms": thinking_duration_ms,
        "truncated": truncated,
    }


async def _persist_qa_history(
    question: str,
    answer: str,
    citations: list[dict],
    channel_id: str,
    user_id: str,
    session_id: str,
    use_v2_schema: bool = False,
    thinking_text: str = "",
    thinking_duration_ms: int | None = None,
    tool_calls: list[dict] | None = None,
) -> None:
    """Write Q&A pair to QAHistoryStore and save messages to ChatHistoryStore.

    Runs as a background task after the SSE stream completes.
    Failures are logged but do not affect the user experience.

    When `use_v2_schema` is True, session is created without top-level channel_id
    and channel_id is stored per-message. Otherwise legacy v1 schema is used.
    """
    from beever_atlas.infra.config import get_settings
    from beever_atlas.stores.qa_history_store import QAHistoryStore
    from beever_atlas.stores.chat_history_store import ChatHistoryStore

    settings = get_settings()

    # Write to QAHistory Weaviate collection — failures are non-fatal
    # QAHistory remains channel-scoped per entry regardless of schema version
    try:
        qa_store = QAHistoryStore(settings.weaviate_url, settings.weaviate_api_key)
        await qa_store.startup()
        await qa_store.write_qa_entry(
            question=question,
            answer=answer,
            citations=citations,
            channel_id=channel_id,
            user_id=user_id,
            session_id=session_id,
        )
        await qa_store.shutdown()
    except Exception:
        logger.exception("Failed to write QA entry to Weaviate for session=%s", session_id)

    # Write to MongoDB chat_history — failures are non-fatal but logged separately
    try:
        chat_store = ChatHistoryStore(settings.mongodb_uri)
        await chat_store.startup()
        thinking_doc = _build_thinking_doc(thinking_text, thinking_duration_ms)
        persisted_tool_calls = tool_calls or None
        if use_v2_schema:
            await chat_store.create_session_v2(
                session_id=session_id, user_id=user_id
            )
            await chat_store.save_message(
                session_id=session_id,
                role="user",
                content=question,
                channel_id=channel_id,
            )
            await chat_store.save_message(
                session_id=session_id,
                role="assistant",
                content=answer,
                citations=citations,
                channel_id=channel_id,
                thinking=thinking_doc,
                tool_calls=persisted_tool_calls,
            )
        else:
            await chat_store.create_session(
                session_id=session_id, channel_id=channel_id, user_id=user_id
            )
            await chat_store.save_message(
                session_id=session_id, role="user", content=question
            )
            await chat_store.save_message(
                session_id=session_id,
                role="assistant",
                content=answer,
                citations=citations,
                thinking=thinking_doc,
                tool_calls=persisted_tool_calls,
            )
        chat_store.close()
    except Exception:
        logger.exception("Failed to persist chat history to MongoDB for session=%s", session_id)


async def _extract_text(content: bytes, mime_type: str, filename: str) -> str:
    """Extract text from uploaded file content."""
    if mime_type in ("text/plain", "text/csv"):
        return content.decode("utf-8", errors="replace")

    if mime_type == "application/pdf":
        try:
            import io
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
            return "\n\n".join(pages)
        except Exception:
            return f"[Could not extract text from {filename}]"

    if mime_type == "application/vnd.openxmlformats-officedocument.wordprocessingml.document":
        try:
            import io
            from docx import Document
            doc = Document(io.BytesIO(content))
            return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
        except Exception:
            return f"[Could not extract text from {filename}]"

    if mime_type.startswith("image/"):
        # Use Gemini vision for image description
        try:
            import google.generativeai as genai
            from beever_atlas.llm.provider import get_llm_provider
            provider = get_llm_provider()
            model_name = provider.resolve_model("qa_router")
            if isinstance(model_name, str):
                model = genai.GenerativeModel(model_name)
                response = await asyncio.to_thread(
                    model.generate_content,
                    [
                        "Describe this image in detail for a knowledge base assistant:",
                        {"mime_type": mime_type, "data": content},
                    ]
                )
                return response.text
        except Exception:
            pass
        return f"[Image: {filename}]"

    return f"[Unsupported content type: {mime_type}]"


@router.get("/api/channels/{channel_id}/ask/history")
async def ask_history(
    channel_id: str,
    request: Request,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
) -> dict:
    """Return paginated past Q&A sessions for the authenticated user.

    Sessions are ordered newest-first. Each entry contains session_id,
    first question preview, and created_at timestamp.
    Supports optional search filtering and excludes soft-deleted sessions.
    """
    from beever_atlas.infra.config import get_settings
    from beever_atlas.stores.chat_history_store import ChatHistoryStore
    from motor.motor_asyncio import AsyncIOMotorClient

    user_id = _extract_user_id(request)
    settings = get_settings()

    # Use direct MongoDB query to support is_deleted filter and search
    client = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db = client["beever_atlas"]
        collection = db["chat_history"]

        skip = (page - 1) * page_size
        query: dict = {
            "channel_id": channel_id,
            "user_id": user_id,
            "is_deleted": {"$ne": True},
        }
        if search:
            escaped_search = re.escape(search)
            query["$or"] = [
                {"title": {"$regex": escaped_search, "$options": "i"}},
                {"messages.content": {"$regex": escaped_search, "$options": "i"}},
            ]

        cursor = (
            collection.find(
                query,
                {
                    "_id": 0,
                    "session_id": 1,
                    "created_at": 1,
                    "title": 1,
                    "pinned": 1,
                    "messages": {"$slice": 1},
                },
            )
            .sort("created_at", -1)
            .skip(skip)
            .limit(page_size)
        )
        sessions = []
        async for doc in cursor:
            first_q = ""
            msgs = doc.get("messages", [])
            if msgs:
                first_q = msgs[0].get("content", "")[:120]
            created = doc.get("created_at")
            sessions.append({
                "session_id": doc["session_id"],
                "created_at": created.isoformat() if hasattr(created, "isoformat") else str(created or ""),
                "first_question": first_q,
                "title": doc.get("title"),
                "pinned": doc.get("pinned", False),
            })
    finally:
        client.close()

    return {"sessions": sessions, "page": page, "page_size": page_size}


@router.post("/api/channels/{channel_id}/ask")
async def ask_channel(
    channel_id: str,
    body: AskRequest,
    request: Request,
) -> StreamingResponse:
    """Stream an ADK agent response as Server-Sent Events.

    Emits: thinking, response_delta, tool_call_start, tool_call_end,
           citations, follow_ups, metadata, error, done.
    """
    user_id = _extract_user_id(request)
    session_id = body.session_id or str(uuid.uuid4())

    return StreamingResponse(
        _run_agent_stream(
            body.question,
            channel_id,
            session_id,
            user_id,
            request,
            mode=body.mode,
            attachments=body.attachments,
            disabled_tools=body.disabled_tools,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/api/channels/{channel_id}/ask/upload")
async def upload_attachment(
    channel_id: str,
    file: UploadFile = FastAPIFile(...),
) -> dict:
    """Upload a file for text extraction. Returns extracted text for injection into agent prompt."""
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size: 10MB")

    mime = file.content_type or ""
    if mime not in SUPPORTED_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    extracted_text = await _extract_text(content, mime, file.filename or "unknown")
    # Truncate to 4000 chars
    if len(extracted_text) > 4000:
        extracted_text = extracted_text[:4000] + "\n\n[... truncated, showing first 4000 characters ...]"

    file_id = str(uuid.uuid4())
    return {
        "file_id": file_id,
        "filename": file.filename,
        "extracted_text": extracted_text,
        "mime_type": mime,
        "size_bytes": len(content),
    }


@router.post("/api/channels/{channel_id}/ask/feedback")
async def submit_feedback(
    channel_id: str,
    body: FeedbackRequest,
    request: Request,
) -> dict:
    """Submit thumbs up/down feedback on an assistant response."""
    from beever_atlas.infra.config import get_settings
    from motor.motor_asyncio import AsyncIOMotorClient

    user_id = _extract_user_id(request)
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db = client["beever_atlas"]

        doc = {
            "session_id": body.session_id,
            "message_id": body.message_id,
            "channel_id": channel_id,
            "user_id": user_id,
            "rating": body.rating,
            "comment": body.comment,
            "created_at": datetime.now(UTC).isoformat(),
        }

        await db.qa_feedback.update_one(
            {"session_id": body.session_id, "message_id": body.message_id},
            {"$set": doc},
            upsert=True,
        )
    finally:
        client.close()

    return {"status": "ok", "feedback": doc}


@router.get("/api/channels/{channel_id}/ask/sessions/{session_id}")
async def get_session(
    channel_id: str,
    session_id: str,
    request: Request,
) -> dict:
    """Load a full conversation session with all messages.

    Authorization: requester must own the session and the URL's channel_id
    must match the session's channel_id (so forged cross-channel URLs 404).
    """
    from beever_atlas.infra.config import get_settings
    from beever_atlas.stores.chat_history_store import ChatHistoryStore
    from fastapi.responses import JSONResponse

    user_id = _extract_user_id(request)
    settings = get_settings()
    store = ChatHistoryStore(settings.mongodb_uri)
    await store.startup()
    try:
        session = await store.load_session(session_id=session_id)
    finally:
        store.close()

    if not session:
        return JSONResponse(status_code=404, content={"error": "Session not found"})  # type: ignore[return-value]

    # Authorization: requester must own the session
    if session.get("user_id") and session["user_id"] != user_id:
        return JSONResponse(status_code=403, content={"error": "Forbidden"})  # type: ignore[return-value]

    # Path validation: URL's channel_id must match the session's channel_id
    # to prevent enumeration via unrelated channel paths
    session_channel = session.get("channel_id")
    if session_channel and session_channel != channel_id:
        return JSONResponse(status_code=404, content={"error": "Session not found"})  # type: ignore[return-value]

    return session


@router.patch("/api/channels/{channel_id}/ask/sessions/{session_id}")
async def update_session(
    channel_id: str,
    session_id: str,
    body: dict,
    request: Request,
) -> dict:
    """Update session metadata (title, pinned status)."""
    from beever_atlas.infra.config import get_settings
    from motor.motor_asyncio import AsyncIOMotorClient

    user_id = _extract_user_id(request)
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db = client["beever_atlas"]

        update_fields = {}
        if "title" in body:
            update_fields["title"] = body["title"]
        if "pinned" in body:
            update_fields["pinned"] = body["pinned"]

        if update_fields:
            await db.chat_history.update_one(
                {"session_id": session_id, "user_id": user_id},
                {"$set": update_fields},
            )
    finally:
        client.close()

    return {"status": "ok", "updated": update_fields}


@router.delete("/api/channels/{channel_id}/ask/sessions/{session_id}")
async def delete_session(
    channel_id: str,
    session_id: str,
    request: Request,
) -> dict:
    """Soft-delete a conversation session."""
    from beever_atlas.infra.config import get_settings
    from motor.motor_asyncio import AsyncIOMotorClient

    user_id = _extract_user_id(request)
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db = client["beever_atlas"]

        await db.chat_history.update_one(
            {"session_id": session_id, "user_id": user_id},
            {"$set": {"is_deleted": True}},
        )
    finally:
        client.close()

    return {"status": "ok"}


# ===========================================================================
# v2 session-scoped endpoints — channel is per-message, not per-session.
# These live alongside the channel-scoped endpoints above for backward compat.
# ===========================================================================


class AskV2Request(BaseModel):
    question: str = Field(..., min_length=1)
    channel_id: str = Field(..., min_length=1, description="Channel to retrieve from for this turn")
    include_citations: bool = Field(default=True)
    max_results: int = Field(default=10, ge=1, le=50)
    session_id: str | None = Field(default=None, description="Resume an existing session")
    mode: str = Field(default="deep", pattern="^(quick|deep|summarize)$")
    attachments: list[dict] = Field(default_factory=list)
    disabled_tools: list[str] = Field(
        default_factory=list,
        description="Per-request tool names to disable. Unknown names are ignored with a warning.",
    )


class FeedbackV2Request(BaseModel):
    session_id: str
    message_id: str
    rating: str = Field(..., pattern="^(up|down)$")
    comment: str | None = None
    channel_id: str | None = None


@router.post("/api/ask")
async def ask_v2(
    body: AskV2Request,
    request: Request,
) -> StreamingResponse:
    """Session-scoped SSE streaming. `channel_id` scopes retrieval for this turn only.

    Sessions are created without a top-level `channel_id`; each message carries
    its own `channel_id`. The derived set of channels used in a session is
    aggregated at read time (see GET /api/ask/sessions/{id}).
    """
    user_id = _extract_user_id(request)
    session_id = body.session_id or str(uuid.uuid4())

    return StreamingResponse(
        _run_agent_stream(
            body.question,
            body.channel_id,
            session_id,
            user_id,
            request,
            mode=body.mode,
            attachments=body.attachments,
            use_v2_schema=True,
            disabled_tools=body.disabled_tools,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/api/ask/tools")
async def get_ask_tools() -> "StreamingResponse":
    """Return the QA tool registry for the tools panel UI.

    Response is cacheable for 5 minutes — the registry is static per deploy.
    """
    from fastapi.responses import JSONResponse
    from beever_atlas.agents.tools import QA_TOOL_DESCRIPTORS

    return JSONResponse(
        content={"tools": list(QA_TOOL_DESCRIPTORS)},
        headers={"Cache-Control": "public, max-age=300"},
    )


@router.get("/api/ask/sessions")
async def list_ask_sessions(
    request: Request,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
) -> dict:
    """List all ask sessions for the authenticated user across all channels."""
    from beever_atlas.infra.config import get_settings
    from beever_atlas.stores.chat_history_store import ChatHistoryStore

    page_size = min(page_size, 50)
    user_id = _extract_user_id(request)
    settings = get_settings()
    store = ChatHistoryStore(settings.mongodb_uri)
    await store.startup()
    try:
        # Fetch one extra to determine whether more pages exist.
        sessions = await store.list_sessions_global(
            user_id=user_id,
            page=page,
            page_size=page_size + 1,
            search=search,
        )
    finally:
        store.close()

    has_more = len(sessions) > page_size
    if has_more:
        sessions = sessions[:page_size]

    return {"sessions": sessions, "page": page, "page_size": page_size, "has_more": has_more}


@router.get("/api/ask/sessions/{session_id}")
async def get_ask_session(
    session_id: str,
    request: Request,
) -> dict:
    """Load a full session with derived channel_ids (v2) or legacy fallback (v1)."""
    from beever_atlas.infra.config import get_settings
    from beever_atlas.stores.chat_history_store import ChatHistoryStore
    from fastapi.responses import JSONResponse

    user_id = _extract_user_id(request)
    settings = get_settings()
    store = ChatHistoryStore(settings.mongodb_uri)
    await store.startup()
    try:
        session = await store.load_session_with_channels(session_id=session_id)
    finally:
        store.close()

    if not session:
        return JSONResponse(status_code=404, content={"error": "Session not found"})  # type: ignore[return-value]

    # Authorization: user can only load their own sessions
    if session.get("user_id") and session["user_id"] != user_id:
        return JSONResponse(status_code=403, content={"error": "Forbidden"})  # type: ignore[return-value]

    return session


@router.patch("/api/ask/sessions/{session_id}")
async def update_ask_session(
    session_id: str,
    body: dict,
    request: Request,
) -> dict:
    """Update session metadata (title, pinned)."""
    from beever_atlas.infra.config import get_settings
    from motor.motor_asyncio import AsyncIOMotorClient

    user_id = _extract_user_id(request)
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db = client["beever_atlas"]
        update_fields: dict = {}
        if "title" in body:
            update_fields["title"] = body["title"]
        if "pinned" in body:
            update_fields["pinned"] = body["pinned"]
        if update_fields:
            await db.chat_history.update_one(
                {"session_id": session_id, "user_id": user_id},
                {"$set": update_fields},
            )
    finally:
        client.close()

    return {"status": "ok", "updated": update_fields}


@router.delete("/api/ask/sessions/{session_id}")
async def delete_ask_session(
    session_id: str,
    request: Request,
) -> dict:
    """Soft-delete an ask session."""
    from beever_atlas.infra.config import get_settings
    from motor.motor_asyncio import AsyncIOMotorClient

    user_id = _extract_user_id(request)
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db = client["beever_atlas"]
        result = await db.chat_history.update_one(
            {"session_id": session_id, "user_id": user_id},
            {"$set": {"is_deleted": True}},
        )
    finally:
        client.close()

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"status": "ok"}


@router.post("/api/ask/upload")
async def upload_ask_attachment(
    file: UploadFile = FastAPIFile(...),
) -> dict:
    """Upload a file for text extraction (channel-less variant)."""
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size: 10MB")

    mime = file.content_type or ""
    if mime not in SUPPORTED_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    extracted_text = await _extract_text(content, mime, file.filename or "unknown")
    if len(extracted_text) > 4000:
        extracted_text = extracted_text[:4000] + "\n\n[... truncated, showing first 4000 characters ...]"

    file_id = str(uuid.uuid4())
    return {
        "file_id": file_id,
        "filename": file.filename,
        "extracted_text": extracted_text,
        "mime_type": mime,
        "size_bytes": len(content),
    }


@router.post("/api/ask/feedback")
async def submit_ask_feedback(
    body: FeedbackV2Request,
    request: Request,
) -> dict:
    """Submit thumbs up/down feedback on an assistant response (channel-less)."""
    from beever_atlas.infra.config import get_settings
    from motor.motor_asyncio import AsyncIOMotorClient

    user_id = _extract_user_id(request)
    settings = get_settings()
    client = AsyncIOMotorClient(settings.mongodb_uri)
    try:
        db = client["beever_atlas"]
        doc = {
            "session_id": body.session_id,
            "message_id": body.message_id,
            "channel_id": body.channel_id,
            "user_id": user_id,
            "rating": body.rating,
            "comment": body.comment,
            "created_at": datetime.now(UTC).isoformat(),
        }
        await db.qa_feedback.update_one(
            {"session_id": body.session_id, "message_id": body.message_id},
            {"$set": doc},
            upsert=True,
        )
    finally:
        client.close()

    return {"status": "ok", "feedback": doc}
