"""Channel and message API endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx as _httpx

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from beever_atlas.adapters import ChannelInfo, get_adapter

router = APIRouter()


class ChannelResponse(BaseModel):
    channel_id: str
    name: str
    platform: str
    is_member: bool = False
    member_count: int | None = None
    topic: str | None = None
    purpose: str | None = None


class MessageResponse(BaseModel):
    content: str
    author: str
    author_name: str = ""
    author_image: str = ""
    platform: str
    channel_id: str
    channel_name: str
    message_id: str
    timestamp: str
    thread_id: str | None = None
    attachments: list[dict[str, Any]] = []
    reactions: list[dict[str, Any]] = []
    reply_count: int = 0
    is_bot: bool = False
    links: list[dict[str, Any]] = []


def _channel_to_response(info: ChannelInfo) -> ChannelResponse:
    return ChannelResponse(
        channel_id=info.channel_id,
        name=info.name,
        platform=info.platform,
        is_member=info.is_member,
        member_count=info.member_count,
        topic=info.topic,
        purpose=info.purpose,
    )


@router.get("/api/channels", response_model=list[ChannelResponse])
async def list_channels() -> list[ChannelResponse]:
    """List all accessible channels."""
    adapter = get_adapter()
    channels = await adapter.list_channels()
    return [_channel_to_response(ch) for ch in channels]


@router.get("/api/channels/{channel_id}", response_model=ChannelResponse)
async def get_channel(channel_id: str) -> ChannelResponse:
    """Get metadata for a specific channel."""
    adapter = get_adapter()
    try:
        info = await adapter.get_channel_info(channel_id)
    except (KeyError, Exception) as e:
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found") from e
    return _channel_to_response(info)


@router.get("/api/channels/{channel_id}/messages", response_model=list[MessageResponse])
async def get_channel_messages(
    channel_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    since: str | None = Query(default=None, description="ISO 8601 datetime filter"),
) -> list[MessageResponse]:
    """Get paginated messages for a channel."""
    adapter = get_adapter()

    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))

    messages = await adapter.fetch_history(channel_id, since=since_dt, limit=limit)

    return [
        MessageResponse(
            content=m.content,
            author=m.author,
            author_name=m.author_name,
            author_image=m.author_image,
            platform=m.platform,
            channel_id=m.channel_id,
            channel_name=m.channel_name,
            message_id=m.message_id,
            timestamp=m.timestamp.isoformat(),
            thread_id=m.thread_id,
            attachments=m.attachments,
            reactions=m.reactions,
            reply_count=m.reply_count,
            is_bot=m.raw_metadata.get("is_bot", False),
            links=m.raw_metadata.get("links", []),
        )
        for m in messages
    ]


@router.get("/api/files/proxy")
async def proxy_file(url: str = Query(..., description="Slack file URL to proxy")):
    adapter = get_adapter()
    if not hasattr(adapter, '_client'):
        raise HTTPException(status_code=501, detail="File proxy not available in mock mode")

    from beever_atlas.infra.config import get_settings
    _settings = get_settings()
    bridge_url = f"{_settings.bridge_url}/bridge/files?url={url}"
    headers = {}
    if _settings.bridge_api_key:
        headers["Authorization"] = f"Bearer {_settings.bridge_api_key}"

    async with _httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(bridge_url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="Failed to fetch file")

        return StreamingResponse(
            iter([resp.content]),
            media_type=resp.headers.get("content-type", "application/octet-stream"),
            headers={"Cache-Control": "public, max-age=3600"},
        )
