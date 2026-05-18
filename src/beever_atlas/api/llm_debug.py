"""Debug endpoints for confirming LLM dispatch state.

Read-only. Surfaces the in-process recent-calls ring buffer (see
``services/llm_call_log.py``) so operators can verify that an Assignment
switch — e.g. "qa_agent → gemini-3.1-flash-lite" — actually reached the
upstream provider, what model id was on the wire, and whether the call
succeeded.

Intentionally NOT included:
  * Request / response message content (privacy).
  * API keys (never recorded into the ring buffer to begin with).
  * Stored in a database (process-local, restarts reset).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query
from pydantic import BaseModel

from beever_atlas.services.llm_call_log import snapshot as _snapshot

router = APIRouter(prefix="/api/settings/debug", tags=["debug"])


class RecentLLMCallResponse(BaseModel):
    ts: str
    kind: str
    consumer: str | None = None
    provider: str
    model: str
    api_base: str | None = None
    latency_ms: int | None = None
    ok: bool
    response_model: str | None = None
    error_class: str | None = None
    error_summary: str | None = None


class RecentLLMCallsResponse(BaseModel):
    calls: list[RecentLLMCallResponse]


@router.get("/recent-llm-calls", response_model=RecentLLMCallsResponse)
async def recent_llm_calls(
    max_age_seconds: float = Query(
        1800.0,
        ge=0,
        description=(
            "RES-284 — drop entries older than this many seconds (default 30 min). "
            "Set to a large value (e.g. 86400) to inspect older calls for debugging."
        ),
    ),
) -> RecentLLMCallsResponse:
    """Return the most recent LLM dispatch calls, newest first.

    Use cases:
      * Confirm an Assignment switch (e.g. ``qa_agent`` → new model) actually
        flowed through to dispatch with the new model id.
      * See ``response_model`` (echoed by the provider) to verify Google
        didn't silently fall back to a different model.
      * Spot failure patterns: ``error_class`` + ``error_summary`` show what
        upstream returned without exposing credentials.

    Bounded to the last 50 calls. Process-local — uvicorn restart resets.
    Filtered by ``max_age_seconds`` to prevent stale failures from haunting
    the Agent Models tab indefinitely (RES-284).
    """
    rows: list[dict[str, Any]] = _snapshot(expires_after_seconds=max_age_seconds)
    return RecentLLMCallsResponse(calls=[RecentLLMCallResponse(**r) for r in rows])


__all__ = ["router"]
