"""SSE streaming Q&A endpoint using ADK Runner."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import AsyncGenerator

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from google.genai import types as genai_types

from beever_atlas.agents import root_agent
from beever_atlas.agents.runner import create_runner, create_session

logger = logging.getLogger(__name__)

router = APIRouter()


class AskRequest(BaseModel):
    question: str = Field(..., min_length=1, description="The question to ask")
    include_citations: bool = Field(default=True)
    max_results: int = Field(default=10, ge=1, le=50)


def _sse_event(event_type: str, data: dict) -> str:
    """Format a Server-Sent Event."""
    return f"event: {event_type}\ndata: {json.dumps(data)}\n\n"


async def _run_agent_stream(
    question: str,
    channel_id: str,
    request: Request,
) -> AsyncGenerator[str, None]:
    """Run the ADK agent and yield SSE events."""
    runner = create_runner(root_agent)
    session = await create_session(user_id="api_user")

    new_message = genai_types.Content(
        role="user",
        parts=[genai_types.Part(text=question)],
    )

    accumulated_text = ""

    try:
        async for event in runner.run_async(
            user_id=session.user_id,
            session_id=session.id,
            new_message=new_message,
        ):
            # Check if client disconnected
            if await request.is_disconnected():
                logger.info("Client disconnected, stopping agent stream")
                break

            # Handle errors
            if event.error_code or event.error_message:
                yield _sse_event("error", {
                    "message": event.error_message or "Unknown error",
                    "code": event.error_code or "AGENT_ERROR",
                })
                return

            # Extract text from event content
            if event.content and event.content.parts:
                for part in event.content.parts:
                    if part.text:
                        # Stream as partial if not turn complete
                        if event.partial:
                            yield _sse_event("response_delta", {"delta": part.text})
                            accumulated_text += part.text
                        else:
                            yield _sse_event("response_delta", {"delta": part.text})
                            accumulated_text += part.text

            # Turn complete — send metadata and done
            if event.turn_complete:
                yield _sse_event("citations", {"items": []})
                yield _sse_event("metadata", {
                    "route": "echo",
                    "confidence": 1.0,
                    "cost_usd": 0.0,
                    "channel_id": channel_id,
                })
                yield _sse_event("done", {})
                return

    except asyncio.CancelledError:
        logger.info("Agent stream cancelled")
        yield _sse_event("error", {
            "message": "Request cancelled",
            "code": "CANCELLED",
        })
    except Exception as e:
        logger.exception("Agent error during streaming")
        yield _sse_event("error", {
            "message": str(e),
            "code": "AGENT_ERROR",
        })


@router.post("/api/channels/{channel_id}/ask")
async def ask_channel(
    channel_id: str,
    body: AskRequest,
    request: Request,
) -> StreamingResponse:
    """Stream an ADK agent response as Server-Sent Events."""
    return StreamingResponse(
        _run_agent_stream(body.question, channel_id, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
