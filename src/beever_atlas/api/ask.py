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

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    UploadFile,
    File as FastAPIFile,
)

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types as genai_types

from beever_atlas.agents.runner import create_runner, create_session
from beever_atlas.infra.rate_limit import limiter

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10MB

# Process-local cache of channel_id -> Slack workspace subdomain (e.g.
# "beever"). Resolved once per channel from the bridge adapter's channel-info
# and reused for the life of the process; keyed by channel_id so it can never
# bleed a domain across channels. Non-Slack channels cache None.
# INVARIANT: one channel_id maps to exactly one Slack workspace for the process
# lifetime (a deployment is workspace-scoped; channel_id→workspace is fixed). If
# multi-workspace-per-deployment is ever introduced, this key — and the
# per-request contextvar stamping — must incorporate the workspace.
_WORKSPACE_DOMAIN_CACHE: dict[str, str | None] = {}


async def _resolve_workspace_domain(channel_id: str) -> str | None:
    """Resolve the Slack workspace subdomain for *channel_id*, cached.

    Returns the cached value when present. Otherwise resolves the channel's
    adapter and reads ``ChannelInfo.workspace_domain``. NEVER raises: any
    error (including a resolution timeout) caches and returns ``None`` — a
    missing domain just means no clickable permalink, never a failed answer.
    The ``get_channel_info`` call is bounded by a short timeout so it can never
    block the answer.
    """
    if channel_id in _WORKSPACE_DOMAIN_CACHE:
        return _WORKSPACE_DOMAIN_CACHE[channel_id]
    try:
        from beever_atlas.api.channels import _resolve_adapter_for_channel

        adapter = await _resolve_adapter_for_channel(channel_id)
        try:
            info = await asyncio.wait_for(adapter.get_channel_info(channel_id), timeout=1.5)
            domain = getattr(info, "workspace_domain", None)
        finally:
            try:
                await adapter.close()
            except Exception:
                pass
        _WORKSPACE_DOMAIN_CACHE[channel_id] = domain
        return domain
    except Exception:
        _WORKSPACE_DOMAIN_CACHE[channel_id] = None
        return None


SUPPORTED_MIME_TYPES = {
    "application/pdf",
    "image/png",
    "image/jpeg",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "text/plain",
    "text/csv",
}


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="The question to ask")
    include_citations: bool = Field(default=True)
    max_results: int = Field(default=10, ge=1, le=50)
    session_id: str | None = Field(default=None, description="Resume an existing session")
    user_id: str | None = Field(
        default=None,
        description=(
            "Acting human user id, asserted by the chat bridge on behalf of the "
            "channel-native user. Used for conversation-memory keying/ACL. When "
            "absent (e.g. web UI), the authenticated principal id is used."
        ),
    )
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


# Inline citation markers, normalized away before comparing two copies of an
# answer — the stateful citation rewriter renumbers the second copy ([1][2] →
# [3][4]) and rewrites [src:xxx] tags, so a byte-identity check would miss a
# real doubling.
_CITE_MARKER_RE = re.compile(r"\[\d+\]|\[src:[^\]]+\]")


def _dedup_exact_double(text: str, session_id: str) -> str:
    """Conservative finalization safety net for verbatim-doubled answers.

    Some streaming paths deliver the full answer twice (a final aggregate that
    repeats the partial stream, or two ADK events). Collapse only when the text
    is a clean doubling — first half equal to second. Citation markers are
    normalized before comparison so a second copy whose ``[N]``/``[src:xxx]``
    tags were renumbered still matches. Requires ≥80 chars; legitimate answers
    are never self-repeats at this length, so this net cannot corrupt a real
    answer.
    """
    n = len(text)
    if n < 80:
        return text
    # Fast path: exact byte doubling (no markers, or identical ones).
    if n % 2 == 0 and text[: n // 2] == text[n // 2 :]:
        logger.warning(
            "dedup: halved exact-duplicate answer (len=%d, session=%s)",
            n,
            session_id,
        )
        return text[: n // 2]
    # Renumbering-aware path: compare with citation markers collapsed so a copy
    # whose [N] tags were renumbered still matches.
    norm = _CITE_MARKER_RE.sub("[#]", text)
    m = len(norm)
    if m >= 80 and m % 2 == 0 and norm[: m // 2] == norm[m // 2 :]:
        # The normalized halves match → the original is firstCopy + renumbered
        # secondCopy. Walk the ORIGINAL, tracking normalized length, to find
        # where the first copy ends (normalized index m // 2), and keep that
        # prefix (with its original — correctly numbered — markers).
        target = m // 2
        norm_len = 0
        i = 0
        while i < n and norm_len < target:
            mt = _CITE_MARKER_RE.match(text, i)
            if mt:
                norm_len += 3  # len("[#]")
                i = mt.end()
            else:
                norm_len += 1
                i += 1
        logger.warning(
            "dedup: halved citation-renumbered duplicate answer (len=%d, session=%s)",
            n,
            session_id,
        )
        return text[:i]
    return text


class _HeartbeatSentinel:
    """Yielded by ``_stream_with_heartbeats`` whenever the underlying
    agent stream has been silent for the heartbeat interval. The QA
    SSE loop translates this into a synthetic ``thinking`` event so
    slow non-Gemini models (Ollama gemma/llama, GLM, …) don't leave
    the UI blank between request-acceptance and first-token-arrival.
    """

    __slots__ = ("elapsed_ms",)

    def __init__(self, elapsed_ms: int) -> None:
        self.elapsed_ms = elapsed_ms


async def _stream_with_heartbeats(stream, interval_seconds: float = 4.0):
    """Async-iterate ``stream`` and yield a :class:`_HeartbeatSentinel`
    every ``interval_seconds`` of silence.

    Why this exists
    ---------------
    Gemini-via-ADK emits ``thought`` parts during reasoning so the UI can
    show "Thought for X.Xs ›" while the model is working. LiteLLM-routed
    models (Ollama, GLM, OpenAI compat shims, …) have no thought channel —
    the SSE connection is dead silent until the first token. On slow local
    inference (gemma4:e4b, llama3.2 on CPU) that gap can be 30-60 seconds,
    during which the user sees nothing and assumes the system has hung.

    The heartbeat fires only until the first real event arrives — once
    the model starts producing tool calls or text deltas, the natural
    event stream takes over. After that, ADK / Gemini's own thinking
    pipeline handles progress reporting unchanged.

    Cancellation: the wrapper's task cancellation is propagated to the
    underlying iterator via ``aclose()`` so an SSE-client disconnect
    cleanly stops both the agent run and the heartbeat ticking.
    """
    aiter = stream.__aiter__()
    started_at = time.monotonic()
    # PEP 479: catching ``StopAsyncIteration`` inside an async generator and
    # ``return``ing converts the implicit StopIteration into a RuntimeError.
    # Use ``anext(default=…)`` so iterator exhaustion surfaces as a sentinel
    # value, no exception involved.
    _STREAM_END = object()
    # ``asyncio.wait_for`` cancels its inner coroutine on timeout, which
    # would corrupt the async iterator (we'd be cancelling the underlying
    # __anext__ mid-flight every heartbeat interval). Instead, launch one
    # long-lived task per pending fetch and ``shield`` it so timeouts only
    # interrupt the WAIT, not the fetch itself.
    pending: asyncio.Task | None = None
    try:
        while True:
            if pending is None:
                pending = asyncio.ensure_future(anext(aiter, _STREAM_END))
            try:
                event = await asyncio.wait_for(asyncio.shield(pending), timeout=interval_seconds)
            except asyncio.TimeoutError:
                # ``pending`` keeps running — reused on the next iteration.
                elapsed_ms = int((time.monotonic() - started_at) * 1000)
                yield _HeartbeatSentinel(elapsed_ms)
                continue
            pending = None  # consumed; fresh task next iteration
            if event is _STREAM_END:
                return
            yield event
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
        aclose = getattr(aiter, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001
                pass


def _extract_citations_from_text(text: str) -> list[dict]:
    """Extract citation-format lines from agent response text.

    Recognises two citation shapes:

    1. ``channel_fact``: ``[N] Author: @handle | Channel: #name | Time: ts``
       — the historical chat-message citation.
    2. ``wiki_page``: ``[N] Wiki Page: <slug> | Section: <id>`` — the new
       per-page wiki citation introduced by the production-wiring
       redesign so wiki-content answers can carry a navigable reference.

    Mixed citations within one answer are supported; the returned list
    preserves textual order. Channel-fact citations that include the
    literal substring ``"Wiki Page:"`` would otherwise misclassify, so we
    match wiki-page citations FIRST and mask their spans before scanning
    for channel-fact citations.
    """
    citations: list[dict] = []
    consumed: list[tuple[int, int]] = []
    for match in re.finditer(
        r"\[(\d+)\]\s+Wiki Page:\s*([^|]+)\|?\s*(?:Section:\s*([^\[\n]+))?",
        text,
    ):
        citations.append(
            {
                "type": "wiki_page",
                "text": match.group(0).strip(),
                "number": match.group(1),
                "page_id": match.group(2).strip() if match.group(2) else "",
                "section_id": match.group(3).strip() if match.group(3) else "",
            }
        )
        consumed.append(match.span())

    # Mask out wiki-page matches so the channel-fact regex can't double-match.
    if consumed:
        chars = list(text)
        for start, end in consumed:
            for i in range(start, end):
                chars[i] = " "
        scrubbed = "".join(chars)
    else:
        scrubbed = text

    for match in re.finditer(
        r"\[(\d+)\]\s+Author:\s*([^|]+)\|?\s*(?:Channel:\s*([^|]+)\|?)?\s*(?:Time:\s*([^\[]+))?",
        scrubbed,
    ):
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
        plan.is_simple,
        len(plan.internal_queries),
        len(plan.external_queries),
        question[:80],
    )
    if plan.is_simple:
        return f"[Channel: {channel_id}]\n\n{question}", None

    # For complex questions, hint the agent about sub-queries so it can
    # plan tool calls more efficiently.
    sub_q_lines = "\n".join(f"  - [{sq.focus}] {sq.query}" for sq in plan.internal_queries)
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


async def _load_chat_history_parts(
    session_id: str,
    user_id: str | None = None,
    channel_id: str | None = None,
) -> list[genai_types.Content]:
    """Load last 10 turns from ChatHistoryStore as genai Content objects.

    ACL-scoped: conversation memory must never cross users or channels. When
    *user_id* / *channel_id* are supplied we verify the stored session
    actually belongs to this (user, channel) before returning any turns — a
    guessed or stale ``session_id`` from another user or another channel is
    treated as a cold start (returns ``[]``), not a memory leak. The check
    fails safe: any lookup error → cold start.
    """
    try:
        # Phase 2 of #31 — use the shared singleton instead of per-request
        # ChatHistoryStore construction. The singleton was started at app
        # startup; do not call startup()/close() here.
        from beever_atlas.stores import get_stores

        store = get_stores().chat_history

        # Memory ACL — only enforced when we know who is asking and where.
        if user_id is not None or channel_id is not None:
            try:
                session_doc = await store.load_session_with_channels(session_id)
            except Exception:
                session_doc = None
            if session_doc is None:
                # Unknown session id → legitimate first message; cold start.
                return []
            # Fail CLOSED: when we know who is asking, the stored session must
            # provably belong to this user — an unset/mismatched owner is denied,
            # not waved through (a real session always persists user_id).
            owner = session_doc.get("user_id")
            if user_id is not None and owner != user_id:
                logger.warning(
                    "memory ACL: cross/indeterminate-user history load denied (session=%s)",
                    session_id,
                )
                return []
            # Fail CLOSED on channel too: a channel-scoped request must provably
            # match the session's channel(s). If we can't establish provenance
            # (no channel ids on the doc), refuse rather than leak.
            if channel_id is not None:
                doc_channels = set(session_doc.get("channel_ids") or [])
                top_channel = session_doc.get("channel_id")
                if top_channel:
                    doc_channels.add(top_channel)
                if channel_id not in doc_channels:
                    logger.warning(
                        "memory ACL: cross/indeterminate-channel history load denied (session=%s)",
                        session_id,
                    )
                    return []

        messages = await store.get_context_messages(session_id=session_id)

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


_META_RECALL_PATTERNS = (
    "what did i ask",
    "what did i just ask",
    "what was my question",
    "what did we discuss",
    "what did we talk about",
    "what have we talked about",
    "summarize our",
    "summarise our",
    "summarize this conversation",
    "summarise this conversation",
    "summarize our conversation",
    "recap our",
    "recap this conversation",
    "do you remember what",
    "remind me what i",
    "my previous question",
    "my last question",
    "my earlier question",
    "what did you just say",
    "what did you tell me",
)


_ACTING_USER_ID_RE = re.compile(r"^[\w.:@|=-]{1,128}$")


def _resolve_acting_user_id(asserted: str | None, principal_id: str) -> str:
    """Resolve the acting human id for conversation-memory keying.

    The chat bridge asserts the channel-native user's id in the request body;
    the web UI omits it and falls back to the authenticated principal. We
    validate the asserted value (bounded length, safe charset) so it can't be
    used to inject into store keys or logs, and fall back to the principal on
    anything unexpected.

    NOTE (security follow-up): this trusts any caller holding a valid service
    key to assert a user id. In the current deployment only the bridge/web
    hold BEEVER_API_KEYS, so the blast radius is limited to memory continuity
    (channel data is separately gated). A stricter model gates this behind a
    dedicated bridge principal (require_bridge) — tracked for a follow-up.
    """
    asserted = (asserted or "").strip()
    if asserted and _ACTING_USER_ID_RE.match(asserted):
        return asserted
    if asserted:
        logger.warning("ask: ignoring malformed acting user_id; using principal")
    return principal_id


def _is_meta_recall_question(question: str) -> bool:
    """Heuristic: True if *question* is about the conversation itself.

    Meta/recall questions ("what did I ask you?", "summarize our chat") should
    be answered from the injected ``<prior_conversation>`` turns, not routed to
    channel retrieval (which dead-ends on an empty state). Conservative on
    purpose: a missed meta question merely falls back to normal retrieval; we
    avoid false positives that would suppress legitimate channel lookups.
    """
    if not question:
        return False
    lowered = question.lower()
    return any(p in lowered for p in _META_RECALL_PATTERNS)


def _compute_confidence(registry) -> float:
    """Honest answer confidence in [0.1, 0.95] from retrieval signals.

    No extra model call — blends signals already collected on the citation
    registry:
      - coverage (40%): how much of the answer is actually cited
        (referenced markers / registered sources);
      - breadth (30%): how many distinct sources were found (4+ = full);
      - quality (30%): average retrieval score of those sources.
    Returns a low ``0.15`` when nothing was retrieved or the registry is off, so
    the signal is honest rather than the old fabricated ``0.85`` constant.
    """
    if registry is None:
        return 0.15
    registered = registry.registered_count
    if registered == 0:
        return 0.15
    coverage = min(1.0, registry.referenced_count / registered)
    breadth = min(1.0, registered / 4.0)
    scores = registry.retrieval_scores()
    quality = sum(scores) / len(scores) if scores else 0.5
    quality = max(0.0, min(1.0, quality))
    blended = 0.4 * coverage + 0.3 * breadth + 0.3 * quality
    return round(max(0.1, min(0.95, blended)), 2)


async def _related_context_payload(
    channel_id: str, principal_id: str, answer_text: str
) -> dict | None:
    """Best-effort inline proactive context (tensions relevant to the answer).

    Computed AFTER the answer has finished streaming, so it never delays the
    visible reply, and time-bounded to 0.8s so a slow wiki scan can't stall the
    trailing events. Returns ``None`` (emit nothing) when there's nothing
    relevant or on any error.
    """
    try:
        from beever_atlas.capabilities.proactive import get_relevant_tensions

        tensions = await asyncio.wait_for(
            get_relevant_tensions(channel_id, principal_id, answer_text, limit=2),
            timeout=0.8,
        )
    except Exception:  # pragma: no cover - defensive; proactive context is best-effort
        return None
    return {"tensions": tensions} if tensions else None


async def _build_metadata_event(
    *,
    channel_id: str,
    session_id: str,
    mode: str,
    registry,
) -> dict:
    """Assemble the SSE ``metadata`` payload.

    Centralized so the non-deep and deep emit paths cannot drift. Adds two
    additive, backward-compatible signals consumed by the chat bot:

    - ``is_empty_retrieval``: ``True`` when retrieval registered no sources, so
      the client can render an honest "nothing indexed" state instead of
      guessing from the answer text. Falls back to ``False`` when the citation
      registry is off (legacy path), leaving the client heuristic in charge.
    - ``last_sync_ts``: the timestamp of the last synced MESSAGE in this
      channel (ISO-8601), NOT the wall-clock time of the last sync RUN. It
      answers "how recent is the newest message I've indexed", which is the
      honest freshness signal the bot surfaces. The accompanying
      ``freshness_kind: "last_message"`` key names this semantics explicitly so
      clients never mistake it for a sync-run time. A store hiccup degrades the
      value to ``None`` rather than breaking the stream — freshness is
      best-effort.
    """
    from beever_atlas.stores import get_stores

    last_sync_ts: str | None = None
    try:
        _state = await get_stores().mongodb.get_channel_sync_state(channel_id)
        if _state is not None:
            last_sync_ts = _state.last_sync_ts
    except Exception:  # pragma: no cover - defensive; freshness is best-effort
        last_sync_ts = None

    return {
        "route": "qa_agent",
        "confidence": _compute_confidence(registry),
        "cost_usd": 0.0,
        "channel_id": channel_id,
        "session_id": session_id,
        "mode": mode,
        "is_empty_retrieval": registry is not None and registry.registered_count == 0,
        "last_sync_ts": last_sync_ts,
        # Names the semantics of ``last_sync_ts`` so the bot can label it
        # honestly ("last message seen", not "last synced"). Additive.
        "freshness_kind": "last_message",
    }


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
    principal_id: str | None = None,
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
                logger.warning("Ignoring unknown tool name in disabled_tools: %r", name)
        if effective_disabled:
            # Build a NEW list — never mutate QA_TOOLS.
            filtered = [t for t in QA_TOOLS if _tool_name(t) not in effective_disabled]
            refusal_clause = (
                "\n\nThe following tools are disabled for this request: "
                f"{', '.join(effective_disabled)}. If answering the question "
                "requires any of them, politely refuse and name the disabled tool(s)."
            )
            agent = create_qa_agent(
                mode=mode,
                tools=filtered,
                extra_instruction=refusal_clause,
                disabled_names=set(effective_disabled),
            )
        else:
            agent = get_agent_for_mode(mode)
    else:
        agent = get_agent_for_mode(mode)
    runner = create_runner(agent)
    session = await create_session(user_id=user_id)

    # Log the resolved model + endpoint at QA invocation so operators can
    # confirm — without polling debug endpoints — which pluggable provider
    # is about to serve this Ask. The per-call ``llm call ok/FAIL`` log
    # from ``llm_call_log`` follows once LiteLLM completes. Together they
    # form the operator-visible trace: "what will run" → "what actually ran".
    try:
        from beever_atlas.llm.provider import get_llm_provider

        _qa_model = get_llm_provider().get_model_string("qa_agent")
        logger.info(
            "qa_ask start: session=%s mode=%s consumer=qa_agent resolved_model=%s",
            session_id,
            mode,
            _qa_model,
        )
    except Exception:  # noqa: BLE001 — never block an Ask on logging
        pass

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
    # Always strip leftover [src:...] literals that the LLM may hallucinate
    # using tool names, regardless of the registry flag. When the registry
    # is enabled below, this default is replaced with the full StreamRewriter.
    from beever_atlas.agents.query.stream_rewriter import LiteralSrcStripper

    _rewriter = LiteralSrcStripper()
    # Principal bind for orchestration tools (openspec atlas-mcp-server
    # Phase 6): the QA agent's orchestration_tools read the principal from
    # this contextvar. Set just before the runner runs and reset in the
    # finally below so tool invocations resolve to a live principal.
    _principal_token = None
    # Per-request Slack workspace subdomain bound into the citation decorator's
    # contextvar so tool outputs get clickable permalinks. Bound just before
    # the runner runs and reset in the finally below to avoid cross-channel
    # leakage between concurrent asks.
    _ws_token = None
    # Channel-isolation: the set of channels this turn may query. v1 binds the
    # @mention channel only — the @mention proves the asking user is a member of
    # it. Every retrieval/graph/list tool refuses channels outside this set.
    _authorized_token = None
    if _registry_enabled:
        from beever_atlas.agents.citations import registry as _citation_registry_mod
        from beever_atlas.agents.citations.permalink_resolver import default_resolver
        from beever_atlas.agents.query.follow_ups_tool import bind_collector
        from beever_atlas.agents.query.stream_rewriter import StreamRewriter

        _registry, _registry_token = _citation_registry_mod.bind(session_id=session_id)
        _registry.set_permalink_resolver(default_resolver)
        _follow_ups_collector, _follow_ups_token = bind_collector()
        _rewriter = StreamRewriter(_registry)

    # Task 4.8: Load prior conversation turns so agent has continuity.
    # ACL-scoped to (user, channel) so memory never crosses users/channels.
    history_parts = await _load_chat_history_parts(session_id, user_id, channel_id)

    # Task 4.3: Decompose question and annotate prompt for complex questions
    prompt_text, _decomposition_plan = await _build_decomposed_prompt(question, channel_id)

    # Inject attachment content. The `User-attached file` heading is the
    # cue the QA prompt keys off (see TOOL_SELECTION_HINTS in prompts.py)
    # to distinguish a user upload from channel-stored media.
    if attachments:
        attachment_sections = []
        for att in attachments:
            attachment_sections.append(
                f"## User-attached file: {att.get('filename', 'unknown')}\n"
                f"(The user uploaded this file in this turn. Questions about "
                f'"this image/file/document" refer to the content below.)\n'
                f"{att.get('extracted_text', '')}"
            )
        prompt_text += "\n\n" + "\n\n".join(attachment_sections)

    # Lean metadata list that gets persisted on the user message so the
    # chip UI re-renders on reload. `extracted_text` is intentionally
    # dropped to keep chat_history docs small.
    persisted_attachments: list[dict] = [
        {
            "file_id": att.get("file_id"),
            "filename": att.get("filename"),
            "mime_type": att.get("mime_type"),
            "size_bytes": att.get("size_bytes"),
        }
        for att in (attachments or [])
    ]

    # Inject history context as a text prefix when prior turns exist
    if history_parts:
        history_lines = []
        for h in history_parts:
            role_label = "User" if h.role == "user" else "Assistant"
            text = h.parts[0].text if h.parts else ""
            history_lines.append(f"[{role_label}]: {text}")
        history_ctx = "\n".join(history_lines)
        # Meta/recall questions ("what did I ask you?", "summarize our chat")
        # are about the conversation itself — answer them from the turns above,
        # not from channel retrieval (which dead-ends on an empty state). Prime
        # the agent so it reads <prior_conversation> instead of calling tools.
        intent_hint = ""
        if _is_meta_recall_question(question):
            intent_hint = (
                "\n\n[NOTE: This question is about our conversation itself. Answer "
                "directly from <prior_conversation> above — do NOT call channel "
                "retrieval tools for it.]"
            )
        prompt_text = (
            f"<prior_conversation>\n{history_ctx}\n</prior_conversation>{intent_hint}\n\n"
            + prompt_text
        )

    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=prompt_text)],
    )

    accumulated_text = ""
    accumulated_thinking = ""
    # Defensive dedup state: track the last emitted chunk and the tail of the
    # accumulated stream so a verbatim-repeated response_delta (seen on some
    # skill-tool / Gemini-planner paths) can be suppressed before reaching
    # the client.  See Ask-page v2 polish notes.
    _last_emitted_chunk: str = ""
    _DEDUP_TAIL_WINDOW = 400
    # Repeat tool-call suppression: log a warning when the agent invokes the
    # same tool with identical args back-to-back within a single turn.
    _last_tool_sig: tuple[str, str] | None = None
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
        yield _sse_event(
            "decomposition",
            {
                "internal": [
                    {"label": sq.focus, "query": sq.query}
                    for sq in _decomposition_plan.internal_queries
                ],
                "external": [
                    {"label": sq.focus, "query": sq.query}
                    for sq in _decomposition_plan.external_queries
                ],
            },
        )

    try:
        # Bind the principal for the orchestration tools' contextvar so
        # list_connections_tool, trigger_sync_tool, etc. resolve to a live
        # principal during this agent turn.
        try:
            from beever_atlas.agents.tools.orchestration_tools import bind_principal

            # Tool ACL binds the AUTHENTICATED principal — NOT the platform
            # user id used for memory keying. The bridge asserts a platform
            # user_id (e.g. "U1") for conversation continuity, but connection/
            # channel/sync tools must still resolve against the real owner
            # principal, or they'd see an empty connection list. Falls back to
            # the session user (web path, where they're the same).
            _principal_token = bind_principal(principal_id or session.user_id)
        except Exception:
            logger.warning(
                "failed to bind principal for orchestration tools",
                exc_info=True,
            )
        # Channel-isolation: constrain every retrieval/graph/list tool to the
        # @mention channel for this turn (defence-in-depth behind the prompt's
        # boundary check). Bound BEFORE runner.run_async so tool invocations
        # see it; reset in the finally below.
        try:
            from beever_atlas.agents.tools.orchestration_tools import (
                bind_authorized_channels,
            )

            _authorized_token = bind_authorized_channels({channel_id})
        except Exception:
            logger.warning(
                "failed to bind authorized channels for tool isolation",
                exc_info=True,
            )
        # Bind the Slack workspace subdomain BEFORE runner.run_async so tool
        # invocations (which run inside the runner) see it via the contextvar.
        try:
            from beever_atlas.agents.tools._citation_decorator import (
                bind_workspace_domain,
            )

            _ws_token = bind_workspace_domain(await _resolve_workspace_domain(channel_id))
        except Exception:
            logger.warning(
                "failed to bind workspace domain for citations",
                exc_info=True,
            )
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
        _heartbeat_synthetic_thinking_open = False
        _seen_real_event = False
        async for event in _stream_with_heartbeats(_stream, interval_seconds=4.0):
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping agent stream")
                break

            # Heartbeat tick — synthetic ``thinking`` event so the UI shows
            # "Thought for X.Xs ›" with a live-ticking timer while the
            # model is silent. Only meaningful BEFORE the first real event
            # arrives; once tool calls or text deltas start, the natural
            # stream handles progress.
            if isinstance(event, _HeartbeatSentinel):
                if not _seen_real_event:
                    _heartbeat_synthetic_thinking_open = True
                    yield _sse_event(
                        "thinking",
                        {
                            "text": "",
                            "elapsed_ms": event.elapsed_ms,
                            "synthetic": True,
                        },
                    )
                continue

            # First real event after one or more heartbeats — close the
            # synthetic thinking indicator before processing the event so
            # the UI cleanly transitions from "Working…" to actual output.
            if _heartbeat_synthetic_thinking_open and not _seen_real_event:
                yield _sse_event(
                    "thinking_done",
                    {"duration_ms": None, "synthetic": True},
                )
                _heartbeat_synthetic_thinking_open = False
            _seen_real_event = True

            if event.error_code or event.error_message:
                yield _sse_event(
                    "error",
                    {
                        "message": event.error_message or "Unknown error",
                        "code": event.error_code or "AGENT_ERROR",
                    },
                )
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
                    # Repeat tool-call suppression: warn if same tool+args ran twice in a row.
                    try:
                        _sig = (
                            tool_name,
                            json.dumps(normalized_input, sort_keys=True, default=str),
                        )
                    except Exception:
                        _sig = (tool_name, str(normalized_input))
                    if _last_tool_sig is not None and _sig == _last_tool_sig:
                        logger.warning(
                            "repeat tool call detected: %s with identical args (session=%s)",
                            tool_name,
                            session_id,
                        )
                    _last_tool_sig = _sig
                    persisted_tool_calls.append(
                        {
                            "tool_name": tool_name,
                            "input": normalized_input,
                            "status": "running",
                        }
                    )
                    yield _sse_event(
                        "tool_call_start",
                        {
                            "tool_name": tool_name,
                            "input": normalized_input,
                        },
                    )

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

                yield _sse_event(
                    "tool_call_end",
                    {
                        "tool_name": tool_name,
                        "result_summary": result_summary,
                        "latency_ms": latency_ms,
                        "facts_found": facts_found,
                    },
                )

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
                # Event-scoped dedup guard: a single NON-partial (aggregate)
                # event must not emit two verbatim-equivalent text parts. The
                # cross-event _last_emitted_chunk guard misses this because the
                # stateful StreamRewriter renumbers citations, so the 2nd copy
                # is no longer byte-identical to the 1st. This flag is reset per
                # event and never throttles the legitimate multi-chunk partial
                # streaming path (partial events are exempt below).
                _emitted_text_this_event = False
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
                        # Event-scoped dedup: on a NON-partial aggregate event,
                        # only the first text part is the answer; any further
                        # text part in the SAME event is a verbatim repeat
                        # (citation-renumbered, so byte-compare guards miss it).
                        # Partial events are exempt — multiple token chunks per
                        # turn are legitimate there.
                        if not _event_is_partial and _emitted_text_this_event:
                            logger.warning(
                                "dedup: suppressed extra text part in same event "
                                "(len=%d, session=%s)",
                                len(part.text),
                                session_id,
                            )
                            continue
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
                                # Defensive dedup: skip verbatim repeats.
                                if rewritten == _last_emitted_chunk or (
                                    len(rewritten) >= 40 and accumulated_text.endswith(rewritten)
                                ):
                                    logger.warning(
                                        "dedup: skipped duplicate response_delta "
                                        "(len=%d, session=%s)",
                                        len(rewritten),
                                        session_id,
                                    )
                                else:
                                    yield _sse_event("response_delta", {"delta": rewritten})
                                    accumulated_text += rewritten
                                    _last_emitted_chunk = rewritten
                                    _emitted_text_this_event = True
                        else:
                            if part.text == _last_emitted_chunk or (
                                len(part.text) >= 40 and accumulated_text.endswith(part.text)
                            ):
                                logger.warning(
                                    "dedup: skipped duplicate response_delta (len=%d, session=%s)",
                                    len(part.text),
                                    session_id,
                                )
                            else:
                                yield _sse_event("response_delta", {"delta": part.text})
                                accumulated_text += part.text
                                _last_emitted_chunk = part.text
                                _emitted_text_this_event = True

            # Turn complete
            if event.turn_complete:
                # Flush any buffered text in the rewriter (mid-tag remainders).
                if _rewriter is not None:
                    tail = _rewriter.flush()
                    if tail:
                        yield _sse_event("response_delta", {"delta": tail})
                        accumulated_text += tail

                # Finalization safety net: collapse a verbatim-doubled answer
                # before citations/persistence. Conservative (exact halves
                # ≥80 chars only). Runs on the answer body before gallery append.
                accumulated_text = _dedup_exact_double(accumulated_text, session_id)

                # Safety net: if retrieval found media but the LLM skipped the
                # `## Media` section, build and append one from the registry.
                if _registry is not None:
                    from beever_atlas.agents.query.gallery_fallback import (
                        maybe_build_gallery,
                    )

                    _gallery = maybe_build_gallery(_registry, accumulated_text)
                    if _gallery:
                        yield _sse_event("response_delta", {"delta": _gallery})
                        accumulated_text += _gallery

                # Build citations: registry-backed envelope (flag on) or
                # legacy regex-parsed list (flag off).
                if _registry is not None:
                    envelope = _registry.finalize(accumulated_text)
                    citations_payload = envelope.to_dict()
                    # Persist the full envelope (sources + refs + attachments)
                    # so inline media survives across page reloads. Read shim
                    # in chat_history_store normalizes legacy rows to the same
                    # shape.
                    citations = citations_payload
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
                    follow_up_match = re.search(r"FOLLOW_UPS:\s*\[([^\]]*)\]", accumulated_text)
                    if follow_up_match:
                        try:
                            follow_ups = json.loads(f"[{follow_up_match.group(1)}]")
                            accumulated_text = re.sub(
                                r"\n*---\n*FOLLOW_UPS:\s*\[.*?\]", "", accumulated_text
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

                yield _sse_event(
                    "metadata",
                    await _build_metadata_event(
                        channel_id=channel_id,
                        session_id=session_id,
                        mode=mode,
                        registry=_registry,
                    ),
                )
                _rc = await _related_context_payload(channel_id, user_id, accumulated_text)
                if _rc:
                    yield _sse_event("related_context", _rc)
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
                    attachments=persisted_attachments,
                )
                yield _sse_event("done", {})
                done_sent = True
                return

    except asyncio.CancelledError:
        logger.info("Agent stream cancelled")
        yield _sse_event(
            "error",
            {
                "message": "Request cancelled",
                "code": "CANCELLED",
            },
        )
        done_sent = True
    except Exception as e:
        logger.exception("Agent error during streaming")
        yield _sse_event(
            "error",
            {
                "message": str(e),
                "code": "AGENT_ERROR",
            },
        )
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
        if _principal_token is not None:
            try:
                from beever_atlas.agents.tools.orchestration_tools import reset_principal

                reset_principal(_principal_token)
            except Exception:
                logger.warning("failed to reset principal", exc_info=True)
        # Reset the authorized-channels contextvar so a turn's channel scope
        # never leaks into a concurrent ask for a different channel (security).
        if _authorized_token is not None:
            try:
                from beever_atlas.agents.tools.orchestration_tools import (
                    reset_authorized_channels,
                )

                reset_authorized_channels(_authorized_token)
            except Exception:
                logger.warning("failed to reset authorized channels", exc_info=True)
        # Reset the workspace-domain contextvar so a domain never leaks across
        # concurrent asks for different channels (correctness/security).
        if _ws_token is not None:
            try:
                from beever_atlas.agents.tools._citation_decorator import (
                    reset_workspace_domain,
                )

                reset_workspace_domain(_ws_token)
            except Exception:
                logger.warning("failed to reset workspace domain", exc_info=True)

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
            if _registry is not None and _rewriter is not None:
                _tail = _rewriter.flush()
                if _tail:
                    yield _sse_event("response_delta", {"delta": _tail})
                    accumulated_text += _tail
                # Finalization safety net (mirror of the turn_complete path):
                # collapse a verbatim-doubled answer before gallery/citations.
                accumulated_text = _dedup_exact_double(accumulated_text, session_id)
                from beever_atlas.agents.query.gallery_fallback import (
                    maybe_build_gallery,
                )

                _gallery2 = maybe_build_gallery(_registry, accumulated_text)
                if _gallery2:
                    yield _sse_event("response_delta", {"delta": _gallery2})
                    accumulated_text += _gallery2
            if accumulated_text.strip():
                # Finalization safety net for the registry-off sub-path (the
                # registry-on body was already deduped above; re-running on a
                # single copy is a no-op).
                accumulated_text = _dedup_exact_double(accumulated_text, session_id)
                # Flush rewriter + emit envelope when registry is active.
                if _rewriter is not None and _registry is not None:
                    tail = _rewriter.flush()
                    if tail:
                        yield _sse_event("response_delta", {"delta": tail})
                        accumulated_text += tail
                    envelope = _registry.finalize(accumulated_text)
                    # Persist the full envelope so reload re-hydrates media.
                    citations = envelope.to_dict()
                    yield _sse_event("citations", citations)
                else:
                    citations = _extract_citations_from_text(accumulated_text)
                    yield _sse_event("citations", {"items": citations})

                # Extract follow-ups — prefer the tool-collector (Phase 1)
                # and fall back to the legacy prose regex.
                follow_ups: list[str] = []
                if _follow_ups_collector is not None and _follow_ups_collector.questions:
                    follow_ups = list(_follow_ups_collector.questions)
                if not follow_ups:
                    follow_up_match = re.search(r"FOLLOW_UPS:\s*\[([^\]]*)\]", accumulated_text)
                    if follow_up_match:
                        try:
                            follow_ups = json.loads(f"[{follow_up_match.group(1)}]")
                            accumulated_text = re.sub(
                                r"\n*---\n*FOLLOW_UPS:\s*\[.*?\]", "", accumulated_text
                            ).rstrip()
                        except (json.JSONDecodeError, ValueError):
                            pass
                if follow_ups:
                    yield _sse_event("follow_ups", {"suggestions": follow_ups})

                yield _sse_event(
                    "metadata",
                    await _build_metadata_event(
                        channel_id=channel_id,
                        session_id=session_id,
                        mode=mode,
                        registry=_registry,
                    ),
                )
                _rc = await _related_context_payload(channel_id, user_id, accumulated_text)
                if _rc:
                    yield _sse_event("related_context", _rc)
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
                    attachments=persisted_attachments,
                )
            yield _sse_event("done", {})


THINKING_MAX_BYTES = 20 * 1024  # Cap persisted reasoning text per message


def _build_thinking_doc(thinking_text: str, thinking_duration_ms: int | None) -> dict | None:
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
    citations: list[dict] | dict,
    channel_id: str,
    user_id: str,
    session_id: str,
    use_v2_schema: bool = False,
    thinking_text: str = "",
    thinking_duration_ms: int | None = None,
    tool_calls: list[dict] | None = None,
    attachments: list[dict] | None = None,
) -> None:
    """Write Q&A pair to QAHistoryStore and save messages to ChatHistoryStore.

    Runs as a background task after the SSE stream completes.
    Failures are logged but do not affect the user experience.

    When `use_v2_schema` is True, session is created without top-level channel_id
    and channel_id is stored per-message. Otherwise legacy v1 schema is used.
    """
    # Phase 2 of #31 — use the shared StoreClients singleton instead of
    # per-request store construction. The singleton was started at app
    # startup; do not call startup()/shutdown()/close() per request.
    from beever_atlas.stores import get_stores

    stores = get_stores()

    # Write to QAHistory Weaviate collection — failures are non-fatal
    # QAHistory remains channel-scoped per entry regardless of schema version
    try:
        await stores.qa_history.write_qa_entry(
            question=question,
            answer=answer,
            citations=citations,
            channel_id=channel_id,
            user_id=user_id,
            session_id=session_id,
        )
    except Exception:
        logger.exception("Failed to write QA entry to Weaviate for session=%s", session_id)

    # Write to MongoDB chat_history — failures are non-fatal but logged separately
    try:
        chat_store = stores.chat_history
        thinking_doc = _build_thinking_doc(thinking_text, thinking_duration_ms)
        persisted_tool_calls = tool_calls or None
        if use_v2_schema:
            await chat_store.create_session_v2(session_id=session_id, user_id=user_id)
            await chat_store.save_message(
                session_id=session_id,
                role="user",
                content=question,
                channel_id=channel_id,
                attachments=attachments,
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
                session_id=session_id,
                role="user",
                content=question,
                attachments=attachments,
            )
            await chat_store.save_message(
                session_id=session_id,
                role="assistant",
                content=answer,
                citations=citations,
                thinking=thinking_doc,
                tool_calls=persisted_tool_calls,
            )
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
        # Use Gemini vision for image description via the LiteLLM funnel —
        # images pass through ``dispatch_completion`` using OpenAI's
        # multimodal messages shape (data-URL image_url + text).
        try:
            import base64

            from beever_atlas.infra.config import get_settings
            from beever_atlas.services.llm_dispatch import (
                dispatch_completion,
                normalize_litellm_model,
                sniff_provider,
            )

            settings = get_settings()
            api_key = getattr(settings, "google_api_key", "") or ""
            if not api_key:
                logger.warning(
                    "vision: no google_api_key configured; image attachment "
                    "falls back to placeholder (%s)",
                    filename,
                )
                return f"[Image: {filename}]"

            model_name = settings.media_vision_model
            data_url = f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"

            response = await asyncio.wait_for(
                dispatch_completion(
                    provider=sniff_provider(model_name),
                    model=normalize_litellm_model(model_name),
                    messages=[
                        {
                            "role": "user",
                            "content": [
                                {"type": "image_url", "image_url": {"url": data_url}},
                                {
                                    "type": "text",
                                    "text": (
                                        "Describe this image in detail for a "
                                        "knowledge base assistant. Include any "
                                        "visible text, people, objects, charts, "
                                        "diagrams, and overall context."
                                    ),
                                },
                            ],
                        }
                    ],
                ),
                timeout=60,
            )
            text = (response.choices[0].message.content or "").strip()  # type: ignore[index, union-attr]
            if text:
                return text
            logger.warning(
                "vision: empty description for image attachment %s (mime=%s)",
                filename,
                mime_type,
            )
        except Exception:
            logger.exception(
                "vision: image description failed for %s (mime=%s)",
                filename,
                mime_type,
            )
        return f"[Image: {filename}]"

    return f"[Unsupported content type: {mime_type}]"


@router.get("/api/channels/{channel_id}/ask/history")
async def ask_history(
    channel_id: str,
    request: Request,
    page: int = 1,
    page_size: int = 20,
    search: str | None = None,
    principal: Principal = Depends(require_user),
) -> dict:
    """Return paginated past Q&A sessions for the authenticated user.

    Sessions are ordered newest-first. Each entry contains session_id,
    first question preview, and created_at timestamp.
    Supports optional search filtering and excludes soft-deleted sessions.
    """
    await assert_channel_access(principal, channel_id)
    # Phase 2 of #31 — use the shared MongoDB client from StoreClients
    # instead of opening a new AsyncIOMotorClient per request.
    from beever_atlas.stores import get_stores

    user_id = principal.id

    # Use direct MongoDB query to support is_deleted filter and search
    db = get_stores().mongodb.db
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
        sessions.append(
            {
                "session_id": doc["session_id"],
                "created_at": created.isoformat()
                if hasattr(created, "isoformat")
                else str(created or ""),
                "first_question": first_q,
                "title": doc.get("title"),
                "pinned": doc.get("pinned", False),
            }
        )

    return {"sessions": sessions, "page": page, "page_size": page_size}


@router.post("/api/channels/{channel_id}/ask")
async def ask_channel(
    channel_id: str,
    body: AskRequest,
    request: Request,
    principal: Principal = Depends(require_user),
) -> StreamingResponse:
    """Stream an ADK agent response as Server-Sent Events.

    Emits: thinking, response_delta, tool_call_start, tool_call_end,
           citations, follow_ups, metadata, error, done.
    """
    await assert_channel_access(principal, channel_id)
    # Conversation memory is keyed to the acting HUMAN, not the shared bridge
    # service key. The chat bridge authenticated the platform event, so it is
    # trusted to assert the channel-native user's id in ``body.user_id``; we
    # use it for session ownership + memory keying so each human gets their own
    # continuity and one user's history never bleeds into another's. The web UI
    # omits it and falls back to the authenticated principal. Channel access
    # above is still gated on the principal; per-turn channel isolation is
    # enforced separately via the authorized-channels contextvar.
    user_id = _resolve_acting_user_id(body.user_id, principal.id)
    session_id = body.session_id or str(uuid.uuid4())
    # Issue #45 — when the client supplies a session_id, verify it isn't
    # someone else's session before persisting Q&A messages or feedback to
    # it. New session_ids (those not yet in chat_history) pass through
    # silently as legitimate first-message sessions.
    if body.session_id:
        await _assert_session_ownership_or_new(session_id, user_id)

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
            principal_id=principal.id,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _save_upload_blob(
    *, content: bytes, filename: str, mime_type: str, owner_user_id: str
) -> str:
    """Persist raw upload bytes to GridFS and return the minted file_id.

    The blob survives session reload, so the chips in chat history stay
    clickable. Owner is stamped server-side and re-checked on GET.
    """
    # Phase 3 of #31 — shared singleton instead of per-request FileStore.
    from beever_atlas.stores import get_stores

    return await get_stores().file_store.save(
        content=content,
        filename=filename,
        mime_type=mime_type,
        owner_user_id=owner_user_id,
    )


@router.post("/api/channels/{channel_id}/ask/upload")
async def upload_attachment(
    channel_id: str,
    file: UploadFile = FastAPIFile(...),
    principal: Principal = Depends(require_user),
) -> dict:
    """Upload a file for text extraction. Returns extracted text for injection into agent prompt."""
    await assert_channel_access(principal, channel_id)
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size: 10MB")

    mime = file.content_type or ""
    if mime not in SUPPORTED_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    extracted_text = await _extract_text(content, mime, file.filename or "unknown")
    # Truncate to 4000 chars
    if len(extracted_text) > 4000:
        extracted_text = (
            extracted_text[:4000] + "\n\n[... truncated, showing first 4000 characters ...]"
        )

    file_id = await _save_upload_blob(
        content=content,
        filename=file.filename or "unknown",
        mime_type=mime,
        owner_user_id=principal.id,
    )
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
    principal: Principal = Depends(require_user),
) -> dict:
    """Submit thumbs up/down feedback on an assistant response."""
    await assert_channel_access(principal, channel_id)
    user_id = principal.id
    # Issue #45 — verify the session belongs to the caller before upserting
    # feedback. Without this, any authenticated user could submit feedback
    # against another user's session_id (the v2 endpoint already had this
    # check via _verify_session_ownership; v1 was missed).
    await _verify_session_ownership(body.session_id, user_id)
    # Phase 2 of #31 — use the shared MongoDB client from StoreClients.
    from beever_atlas.stores import get_stores

    db = get_stores().mongodb.db

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

    return {"status": "ok", "feedback": doc}


@router.get("/api/channels/{channel_id}/ask/sessions/{session_id}")
async def get_session(
    channel_id: str,
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_user),
) -> dict:
    """Load a full conversation session with all messages.

    Authorization: requester must own the session and the URL's channel_id
    must match the session's channel_id (so forged cross-channel URLs 404).
    """
    await assert_channel_access(principal, channel_id)
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.stores import get_stores
    from fastapi.responses import JSONResponse

    user_id = principal.id
    session = await get_stores().chat_history.load_session(session_id=session_id)

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
    principal: Principal = Depends(require_user),
) -> dict:
    """Update session metadata (title, pinned status)."""
    await assert_channel_access(principal, channel_id)
    # Phase 3 of #31 — shared MongoDB client.
    from beever_atlas.stores import get_stores

    user_id = principal.id
    db = get_stores().mongodb.db

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

    return {"status": "ok", "updated": update_fields}


@router.delete("/api/channels/{channel_id}/ask/sessions/{session_id}")
async def delete_session(
    channel_id: str,
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_user),
) -> dict:
    """Soft-delete a conversation session."""
    await assert_channel_access(principal, channel_id)
    # Phase 3 of #31 — shared MongoDB client.
    from beever_atlas.stores import get_stores

    user_id = principal.id
    db = get_stores().mongodb.db

    await db.chat_history.update_one(
        {"session_id": session_id, "user_id": user_id},
        {"$set": {"is_deleted": True}},
    )

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
@limiter.limit("30/minute")
async def ask_v2(
    request: Request,
    body: AskV2Request,
    principal: Principal = Depends(require_user),
) -> StreamingResponse:
    """Session-scoped SSE streaming. `channel_id` scopes retrieval for this turn only.

    Sessions are created without a top-level `channel_id`; each message carries
    its own `channel_id`. The derived set of channels used in a session is
    aggregated at read time (see GET /api/ask/sessions/{id}).
    """
    await assert_channel_access(principal, body.channel_id)
    user_id = principal.id
    session_id = body.session_id or str(uuid.uuid4())
    # Issue #45 — verify ownership when the client supplies a session_id.
    if body.session_id:
        await _assert_session_ownership_or_new(session_id, user_id)

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
    principal: Principal = Depends(require_user),
) -> dict:
    """List all ask sessions for the authenticated user across all channels."""
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.stores import get_stores

    page_size = min(page_size, 50)
    user_id = principal.id
    # Fetch one extra to determine whether more pages exist.
    sessions = await get_stores().chat_history.list_sessions_global(
        user_id=user_id,
        page=page,
        page_size=page_size + 1,
        search=search,
    )

    has_more = len(sessions) > page_size
    if has_more:
        sessions = sessions[:page_size]

    return {"sessions": sessions, "page": page, "page_size": page_size, "has_more": has_more}


@router.get("/api/ask/sessions/{session_id}")
async def get_ask_session(
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_user),
) -> dict:
    """Load a full session with derived channel_ids (v2) or legacy fallback (v1)."""
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.stores import get_stores
    from fastapi.responses import JSONResponse

    user_id = principal.id
    session = await get_stores().chat_history.load_session_with_channels(session_id=session_id)

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
    principal: Principal = Depends(require_user),
) -> dict:
    """Update session metadata (title, pinned)."""
    # Phase 3 of #31 — shared MongoDB client.
    from beever_atlas.stores import get_stores

    user_id = principal.id
    db = get_stores().mongodb.db
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

    return {"status": "ok", "updated": update_fields}


@router.delete("/api/ask/sessions/{session_id}")
async def delete_ask_session(
    session_id: str,
    request: Request,
    principal: Principal = Depends(require_user),
) -> dict:
    """Soft-delete an ask session."""
    # Phase 3 of #31 — shared MongoDB client.
    from beever_atlas.stores import get_stores

    user_id = principal.id
    db = get_stores().mongodb.db
    result = await db.chat_history.update_one(
        {"session_id": session_id, "user_id": user_id},
        {"$set": {"is_deleted": True}},
    )

    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Session not found")

    return {"status": "ok"}


# ===========================================================================
# Share endpoints — Phase 2 of ask-url-routing-share-chat plan.
# Owner-authenticated CRUD on `shared_conversations`; public GET lives on the
# `public_router` below which is mounted WITHOUT the global auth dep.
# ===========================================================================


_SHARE_VISIBILITIES = {"owner", "auth", "public"}


class ShareCreateRequest(BaseModel):
    visibility: str = Field(default="owner", pattern="^(owner|auth|public)$")


class ShareVisibilityRequest(BaseModel):
    visibility: str = Field(..., pattern="^(owner|auth|public)$")


def _share_response(doc: dict) -> dict:
    created = doc.get("created_at")
    rotated = doc.get("rotated_at")
    updated = doc.get("updated_at")
    return {
        "share_token": doc["share_token"],
        "url": f"/ask/shared/{doc['share_token']}",
        "visibility": doc["visibility"],
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "rotated_at": rotated.isoformat() if hasattr(rotated, "isoformat") else rotated,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


async def _verify_session_ownership(session_id: str, user_id: str) -> dict:
    """Return the session doc or raise 403/404."""
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.stores import get_stores

    session = await get_stores().chat_history.load_session(session_id=session_id)

    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    # Fail-closed: require exact owner match. Legacy sessions without a user_id
    # cannot be shared (would otherwise be claimable by any authenticated caller).
    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return session


async def _assert_session_ownership_or_new(session_id: str, user_id: str) -> bool:
    """Issue #45 — variant of `_verify_session_ownership` for endpoints
    where a NEW session_id is legitimate (the ask endpoints accept
    client-generated UUIDs for first-message sessions).

    Returns True if the session exists and belongs to ``user_id``.
    Returns False if the session doesn't exist (caller should treat as a
    new session).
    Raises 403 if the session exists but belongs to a different user —
    this is the bug class issue #45 closes: client-supplied session_ids
    being used to inject Q&A messages and feedback into another user's
    session document.
    """
    from beever_atlas.stores import get_stores

    session = await get_stores().chat_history.load_session(session_id=session_id)
    if not session:
        return False
    if session.get("user_id") != user_id:
        raise HTTPException(status_code=403, detail="Forbidden")
    return True


@router.post("/api/ask/sessions/{session_id}/share")
async def create_or_rotate_share(
    session_id: str,
    request: Request,
    body: ShareCreateRequest | None = None,
    rotate: bool = False,
    caller_user_id: str = Depends(require_user),
) -> dict:
    """Create a share for the session, or rotate the token on an existing one.

    - No existing active share: create with the requested visibility (default owner).
    - Existing active share AND rotate=False: return it unchanged.
    - Existing active share AND rotate=True: atomic single-doc token rotation.
    """
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.services.share_snapshot import build_share_snapshot
    from beever_atlas.stores import get_stores

    user_id = caller_user_id
    session = await _verify_session_ownership(session_id, user_id)

    visibility = (body.visibility if body else "owner") or "owner"

    store = get_stores().share_store
    existing = await store.get_active_by_session(user_id, session_id)
    if existing and rotate:
        rotated = await store.rotate_token(existing["_id"])
        if rotated is None:
            # Lost the race — someone else rotated/revoked first.
            raise HTTPException(status_code=404, detail="Share no longer active")
        return _share_response(rotated)
    if existing and not rotate:
        return _share_response(existing)

    # Create new
    title = session.get("title") or ""
    scrubbed = build_share_snapshot(session.get("messages") or [])
    doc = await store.create(
        owner_user_id=user_id,
        source_session_id=session_id,
        visibility=visibility,
        title=title,
        messages=scrubbed,
    )
    return _share_response(doc)


@router.put("/api/ask/sessions/{session_id}/share")
async def resnapshot_share(
    session_id: str,
    request: Request,
    caller_user_id: str = Depends(require_user),
) -> dict:
    """Re-snapshot the session into the existing share (token stable)."""
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.services.share_snapshot import build_share_snapshot
    from beever_atlas.stores import get_stores

    user_id = caller_user_id
    session = await _verify_session_ownership(session_id, user_id)

    store = get_stores().share_store
    existing = await store.get_active_by_session(user_id, session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="No active share")
    title = session.get("title") or ""
    scrubbed = build_share_snapshot(session.get("messages") or [])
    updated = await store.resnapshot(existing["_id"], title=title, messages=scrubbed)
    if updated is None:
        raise HTTPException(status_code=404, detail="No active share")
    return _share_response(updated)


@router.patch("/api/ask/sessions/{session_id}/share/visibility")
async def update_share_visibility(
    session_id: str,
    body: ShareVisibilityRequest,
    request: Request,
    caller_user_id: str = Depends(require_user),
) -> dict:
    """Update visibility tier of an existing active share."""
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.stores import get_stores

    user_id = caller_user_id
    await _verify_session_ownership(session_id, user_id)

    store = get_stores().share_store
    existing = await store.get_active_by_session(user_id, session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="No active share")
    updated = await store.update_visibility(existing["_id"], body.visibility)
    if updated is None:
        raise HTTPException(status_code=404, detail="No active share")
    return _share_response(updated)


@router.delete("/api/ask/sessions/{session_id}/share", status_code=204)
async def revoke_share(
    session_id: str,
    request: Request,
    caller_user_id: str = Depends(require_user),
) -> Response:
    """Revoke the active share. Idempotent: 204 on transition, 404 if none active."""
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.stores import get_stores

    user_id = caller_user_id
    await _verify_session_ownership(session_id, user_id)

    store = get_stores().share_store
    existing = await store.get_active_by_session(user_id, session_id)
    if not existing:
        raise HTTPException(status_code=404, detail="No active share")
    transitioned = await store.revoke(existing["_id"])
    if not transitioned:
        raise HTTPException(status_code=404, detail="No active share")
    return Response(status_code=204)


# ---- Public GET (auth optional, lives on the un-auth'd public_router) ----

public_router = APIRouter()

# Per-share and per-IP in-memory rate buckets. Acceptable for v1 single-process
# dev; swap for Redis in prod if horizontal scaling is added.
_RATE_WINDOW_S = 60.0
_RATE_MAX_KEYS = 10000
_rate_state: dict[str, tuple[float, int]] = {}
_rate_lock = asyncio.Lock()


def _rate_prune_expired(now: float) -> None:
    """Drop buckets whose window has fully elapsed. O(n); bounded by _RATE_MAX_KEYS."""
    expired = [k for k, (ws, _c) in _rate_state.items() if now - ws >= _RATE_WINDOW_S]
    for k in expired:
        _rate_state.pop(k, None)


async def _rate_check(key: str, limit: int) -> bool:
    """Simple fixed-window counter. Returns True if under limit (and records the hit).

    Atomic under asyncio concurrency via module-level lock. Evicts expired buckets
    opportunistically to prevent unbounded memory growth.
    """
    now = time.monotonic()
    async with _rate_lock:
        # Opportunistic eviction when the map grows past the soft cap.
        if len(_rate_state) > _RATE_MAX_KEYS:
            _rate_prune_expired(now)
        window_start, count = _rate_state.get(key, (now, 0))
        if now - window_start >= _RATE_WINDOW_S:
            _rate_state[key] = (now, 1)
            return True
        if count >= limit:
            return False
        _rate_state[key] = (window_start, count + 1)
        return True


def _client_ip(request: Request) -> str:
    xff = request.headers.get("x-forwarded-for")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else "unknown"


def _hash_ip(ip: str) -> str:
    import hashlib

    return hashlib.sha256(ip.encode("utf-8")).hexdigest()[:32]


@public_router.get("/api/ask/shared/{share_token}")
async def get_shared_conversation(
    share_token: str,
    request: Request,
):
    """Serve a shared conversation snapshot. Auth is conditional on visibility tier.

    Ordering directive (per plan): revoked/missing tokens must hard-404 BEFORE
    any rate-limit bucket is consulted, otherwise an attacker replaying an old
    token could drain the quota of the new one.
    """
    # Phase 3 of #31 — shared singleton.
    from fastapi.responses import JSONResponse

    # Issue #88 — shared-link visits arrive via `<a href>`-style navigation
    # which may carry `?access_token=`. Use the loader-optional dep so
    # query-string auth still resolves caller identity here, while the
    # default `require_user_optional` (header-only) protects everything else.
    from beever_atlas.infra.auth import require_user_loader_optional
    from beever_atlas.stores import get_stores

    store = get_stores().share_store
    try:
        doc = await store.get_by_token(share_token)
        # Hard-404 BEFORE any rate-limit accounting.
        if not doc or doc.get("revoked_at") is not None:
            return JSONResponse(status_code=404, content={"error": "Not found"})

        # Resolve optional caller identity (no 401 on missing). Issue #89 —
        # the dep now also accepts `?loader_token=` (signed) which short-
        # circuits before the legacy raw-key path.
        caller_principal = require_user_loader_optional(
            request=request,
            authorization=request.headers.get("authorization"),
            access_token=request.query_params.get("access_token"),
            loader_token=request.query_params.get("loader_token"),
        )
        caller_user_id = caller_principal.id if caller_principal is not None else None

        visibility = doc.get("visibility", "owner")
        owner_user_id = doc.get("owner_user_id")

        if visibility == "owner":
            if not caller_user_id:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Authentication required"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
            if caller_user_id != owner_user_id:
                return JSONResponse(status_code=403, content={"error": "Forbidden"})
        elif visibility == "auth":
            if not caller_user_id:
                return JSONResponse(
                    status_code=401,
                    content={"error": "Authentication required"},
                    headers={"WWW-Authenticate": "Bearer"},
                )
        elif visibility == "public":
            # Rate limit only the public tier.
            ip = _client_ip(request)
            # Key on stable share _id (not the rotatable token) so rotation does
            # not reset the per-share bucket.
            if not await _rate_check(f"share:id:{doc['_id']}", 60):
                return JSONResponse(status_code=429, content={"error": "Rate limit exceeded"})
            if not await _rate_check(f"share:ip:{ip}", 120):
                return JSONResponse(status_code=429, content={"error": "Rate limit exceeded"})
            # Append access log (FIFO cap 1000).
            try:
                await store.append_access_log(
                    doc["_id"],
                    {
                        "ip_hash": _hash_ip(ip),
                        "ua": (request.headers.get("user-agent") or "")[:256],
                        "ts": datetime.now(UTC),
                    },
                )
            except Exception:
                logger.debug("append_access_log failed", exc_info=True)
        else:
            return JSONResponse(status_code=404, content={"error": "Not found"})

        created = doc.get("created_at")
        payload = {
            "title": doc.get("title") or "",
            "messages": doc.get("messages") or [],
            "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
            "visibility": visibility,
            "owner_user_id": owner_user_id,
        }
        headers = {
            "Referrer-Policy": "no-referrer",
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "private, no-store",
        }
        return JSONResponse(content=payload, headers=headers)
    finally:
        # Phase 3 of #31 — singleton owns lifecycle; nothing to close here.
        # The try/finally is preserved as a no-op so the diff stays small.
        pass


@router.post("/api/ask/upload")
async def upload_ask_attachment(
    file: UploadFile = FastAPIFile(...),
    principal: Principal = Depends(require_user),
) -> dict:
    """Upload a file for text extraction (channel-less variant).

    Bytes are persisted to GridFS so the resulting `file_id` resolves via
    the `GET /api/ask/files/{file_id}` endpoint — enabling the composer
    chip and past user-message chips to preview images / download docs.
    """
    content = await file.read()
    if len(content) > MAX_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="File too large. Maximum size: 10MB")

    mime = file.content_type or ""
    if mime not in SUPPORTED_MIME_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type")

    extracted_text = await _extract_text(content, mime, file.filename or "unknown")
    if len(extracted_text) > 4000:
        extracted_text = (
            extracted_text[:4000] + "\n\n[... truncated, showing first 4000 characters ...]"
        )

    file_id = await _save_upload_blob(
        content=content,
        filename=file.filename or "unknown",
        mime_type=mime,
        owner_user_id=principal.id,
    )
    return {
        "file_id": file_id,
        "filename": file.filename,
        "extracted_text": extracted_text,
        "mime_type": mime,
        "size_bytes": len(content),
    }


@router.get("/api/ask/files/{file_id}")
async def get_ask_attachment(
    file_id: str,
    principal: Principal = Depends(require_user),
) -> Response:
    """Serve a stored upload blob back to its owner.

    Auth: fail-closed on unknown id (404) or non-owner caller (403). The
    owner check reads `metadata.owner_user_id` off the GridFS file doc —
    the same value stamped at upload time.

    Uploads cap at 10MB (MAX_UPLOAD_SIZE), so a single `read()` into
    memory is acceptable and keeps lifecycle simple (no streaming chunks
    that would keep the Mongo cursor open past the finally block).
    """
    # Phase 3 of #31 — shared singleton.
    from beever_atlas.stores import get_stores

    store = get_stores().file_store
    stream = await store.open(file_id)
    if stream is None:
        raise HTTPException(status_code=404, detail="File not found")

    meta = stream.metadata or {}
    owner = meta.get("owner_user_id")
    if owner and owner != principal.id:
        raise HTTPException(status_code=403, detail="Forbidden")

    mime = meta.get("mime_type") or "application/octet-stream"
    filename = stream.filename or "file"
    data = await stream.read()
    from urllib.parse import quote

    safe_ascii = (
        filename.encode("ascii", "ignore")
        .decode()
        .replace('"', "")
        .replace("\r", "")
        .replace("\n", "")
    ) or "file"
    encoded = quote(filename, safe="")
    # `inline` lets <img src> render images directly; the frontend
    # decides whether to download or preview based on mime_type.
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Content-Disposition": (
                f"inline; filename=\"{safe_ascii}\"; filename*=UTF-8''{encoded}"
            ),
            "Cache-Control": "private, max-age=300",
            "X-Robots-Tag": "noindex, nofollow",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.post("/api/ask/feedback")
async def submit_ask_feedback(
    body: FeedbackV2Request,
    request: Request,
    principal: Principal = Depends(require_user),
) -> dict:
    """Submit thumbs up/down feedback on an assistant response (channel-less)."""
    user_id = principal.id
    # RES-202: verify the session belongs to the caller before upserting
    # feedback. Without this, any authenticated user could overwrite
    # another user's feedback document keyed by (session_id, message_id)
    # and inject arbitrary channel_id attribution into analytics.
    await _verify_session_ownership(body.session_id, user_id)
    # RES-177 M7: when the caller supplies channel_id, enforce ownership.
    # This prevents cross-tenant feedback injection against another user's
    # channels. channel_id is optional in FeedbackV2Request so we only
    # check when it is provided.
    if body.channel_id:
        await assert_channel_access(principal, body.channel_id)

    # Phase 3 of #31 — shared MongoDB client.
    from beever_atlas.stores import get_stores

    db = get_stores().mongodb.db
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

    return {"status": "ok", "feedback": doc}
