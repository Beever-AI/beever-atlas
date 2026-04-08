"""Channel and message API endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

import httpx as _httpx

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from beever_atlas.adapters import ChannelInfo, get_adapter
from beever_atlas.adapters.bridge import BridgeError, ChatBridgeAdapter
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)


def _detect_platform_from_channel_id(channel_id: str) -> str | None:
    """Infer platform from channel ID format to avoid cross-platform API calls."""
    if re.match(r"^[CDG][A-Z0-9]{8,}$", channel_id):
        return "slack"
    if re.match(r"^\d{17,20}$", channel_id):
        return "discord"
    return None

router = APIRouter()


def _get_adapter_for_connection(connection_id: str | None = None) -> ChatBridgeAdapter:
    """Return a connection-scoped adapter if connection_id is given, else the default."""
    if connection_id:
        return ChatBridgeAdapter(connection_id=connection_id)
    base = get_adapter()
    if isinstance(base, ChatBridgeAdapter):
        return base
    return ChatBridgeAdapter()


class ChannelResponse(BaseModel):
    channel_id: str
    name: str
    platform: str
    is_member: bool = False
    member_count: int | None = None
    topic: str | None = None
    purpose: str | None = None
    connection_id: str | None = None


class MessageResponse(BaseModel):
    content: str
    author: str
    author_name: str = ""
    author_image: str | None = None
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


class MessagesListResponse(BaseModel):
    messages: list[MessageResponse]
    total_count: int | None = None


def _channel_to_response(info: ChannelInfo) -> ChannelResponse:
    return ChannelResponse(
        channel_id=info.channel_id,
        name=info.name,
        platform=info.platform,
        is_member=info.is_member,
        member_count=info.member_count,
        topic=info.topic,
        purpose=info.purpose,
        connection_id=info.connection_id,
    )


_PER_CONNECTION_TIMEOUT = 10.0  # seconds — prevent one slow platform from blocking all

# Simple in-memory cache for channel lists to avoid hammering platform APIs
# on every page navigation. Cache is per-connection with a 60s TTL.
import time as _time

_channel_cache: dict[str, tuple[float, list[ChannelInfo]]] = {}
_CHANNEL_CACHE_TTL = 60.0  # seconds


async def _fetch_connection_channels(
    conn_id: str, selected: list[str],
) -> list[ChannelInfo]:
    """Fetch channels for a single connection, filtering by selected_channels.

    Each call creates a short-lived adapter that is closed after use to avoid
    leaking httpx connections.  A per-connection timeout prevents one slow
    platform (e.g. Discord rate limits) from blocking the entire response.
    """
    # Check cache first
    cache_key = conn_id
    cached = _channel_cache.get(cache_key)
    if cached:
        cached_at, cached_channels = cached
        if _time.monotonic() - cached_at < _CHANNEL_CACHE_TTL:
            if selected:
                selected_set = set(selected)
                return [ch for ch in cached_channels if ch.channel_id in selected_set]
            return cached_channels

    adapter = ChatBridgeAdapter(connection_id=conn_id)
    try:
        channels = await asyncio.wait_for(
            adapter.list_channels(),
            timeout=_PER_CONNECTION_TIMEOUT,
        )
        # Cache the unfiltered result
        _channel_cache[cache_key] = (_time.monotonic(), channels)
        # If selected_channels configured, filter to only those
        if selected:
            selected_set = set(selected)
            channels = [ch for ch in channels if ch.channel_id in selected_set]
        return channels
    finally:
        await adapter.close()


@router.get("/api/channels", response_model=list[ChannelResponse])
async def list_channels() -> list[ChannelResponse]:
    """List channels from all connected platform connections.

    Iterates every PlatformConnection with status='connected', fetches
    channels per-connection in parallel, and filters by each connection's
    selected_channels list.  One failing connection does not block the others.
    """
    from beever_atlas.stores import get_stores

    stores = get_stores()
    connections = await stores.platform.list_connections()
    connected = [c for c in connections if c.status == "connected"]

    if not connected:
        return []

    tasks = [
        _fetch_connection_channels(conn.id, conn.selected_channels)
        for conn in connected
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_channels: list[ChannelInfo] = []
    for conn, result in zip(connected, results):
        if isinstance(result, BaseException):
            logger.warning(
                "Failed to fetch channels for connection %s (%s): %s",
                conn.id, conn.display_name, result,
            )
            continue
        all_channels.extend(result)

    return [_channel_to_response(ch) for ch in all_channels]


@router.get("/api/channels/{channel_id}", response_model=ChannelResponse)
async def get_channel(
    channel_id: str,
    connection_id: str | None = Query(default=None),
) -> ChannelResponse:
    """Get metadata for a specific channel.

    When *connection_id* is provided, fetches directly from that connection.
    Otherwise, iterates all connected PlatformConnections until the channel is
    found — this supports direct URL navigation and page refreshes where no
    route state (and therefore no connection_id) is available.
    """
    if connection_id:
        adapter = ChatBridgeAdapter(connection_id=connection_id)
        try:
            info = await adapter.get_channel_info(channel_id)
            return _channel_to_response(info)
        except Exception:
            pass  # Fall through to search all connections
        finally:
            await adapter.close()

    # No connection_id or provided one didn't match — search across connections.
    # Detect likely platform from channel ID format to skip wrong platforms
    # and avoid wasting API calls / rate limit budget.
    from beever_atlas.stores import get_stores

    likely_platform = _detect_platform_from_channel_id(channel_id)

    stores = get_stores()
    connections = await stores.platform.list_connections()
    connected = [c for c in connections if c.status == "connected"]

    # If we know the platform, only try matching connections
    if likely_platform:
        candidates = [c for c in connected if c.platform == likely_platform]
        if not candidates:
            candidates = connected  # fallback to all if no match
    else:
        candidates = connected

    for conn in candidates:
        adapter = ChatBridgeAdapter(connection_id=conn.id)
        try:
            info = await adapter.get_channel_info(channel_id)
            return _channel_to_response(info)
        except (KeyError, BridgeError):
            continue
        except Exception:
            continue
        finally:
            await adapter.close()

    raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")


@router.get("/api/channels/{channel_id}/messages", response_model=MessagesListResponse)
async def get_channel_messages(
    channel_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    since: str | None = Query(default=None, description="ISO 8601 datetime filter"),
    before: str | None = Query(default=None, description="Message ID cursor - fetch messages before this ID"),
    order: str = Query(default="desc", description="Sort order: desc (newest first) or asc (oldest first)"),
    connection_id: str | None = Query(default=None),
) -> MessagesListResponse:
    """Get paginated messages for a channel."""
    adapter = _get_adapter_for_connection(connection_id)

    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))

    try:
        messages = await adapter.fetch_history(channel_id, since=since_dt, limit=limit, before=before, order=order)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found") from e
    except BridgeError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e)) from e

    response_messages = [
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
    total_count = None
    try:
        stores = get_stores()
        sync_state = await stores.mongodb.get_channel_sync_state(channel_id)
        if sync_state is not None and sync_state.total_synced_messages:
            total_count = sync_state.total_synced_messages
    except RuntimeError:
        pass
    # Fall back to live count from bridge if no sync data
    if total_count is None and hasattr(adapter, "fetch_message_count"):
        total_count = await adapter.fetch_message_count(channel_id)
    return MessagesListResponse(
        messages=response_messages,
        total_count=total_count,
    )


@router.get(
    "/api/channels/{channel_id}/threads/{thread_id}/messages",
    response_model=list[MessageResponse],
)
async def get_thread_messages(
    channel_id: str,
    thread_id: str,
    connection_id: str | None = Query(default=None),
) -> list[MessageResponse]:
    """Get all messages in a thread (parent + replies)."""
    adapter = _get_adapter_for_connection(connection_id)
    try:
        messages = await adapter.fetch_thread(channel_id, thread_id)
    except KeyError as e:
        raise HTTPException(
            status_code=404, detail=f"Thread {thread_id} not found"
        ) from e
    except BridgeError as e:
        raise HTTPException(
            status_code=e.status_code or 502, detail=str(e)
        ) from e

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


@router.delete("/api/channels/{channel_id}/data")
async def clear_channel_data(channel_id: str):
    """Delete all synced data (facts, entities, events, media, sync state) for a channel."""
    from beever_atlas.stores import get_stores

    stores = get_stores()
    results: dict[str, Any] = {}

    # Clear Weaviate facts
    try:
        weaviate_deleted = await stores.weaviate.delete_by_channel(channel_id)
        results["weaviate_facts_deleted"] = weaviate_deleted
    except Exception as exc:
        results["weaviate_error"] = str(exc)

    # Clear Neo4j entities, events, media
    try:
        neo4j_results = await stores.graph.delete_channel_data(channel_id)
        results.update(neo4j_results)
    except Exception as exc:
        results["neo4j_error"] = str(exc)

    # Clear MongoDB sync state
    try:
        await stores.mongodb.clear_channel_sync_state(channel_id)
        results["sync_state_cleared"] = True
    except Exception as exc:
        results["mongodb_error"] = str(exc)

    return results


@router.get("/api/files/proxy")
async def proxy_file(
    url: str = Query(..., description="File URL to proxy"),
    connection_id: str | None = Query(None, description="Connection ID for multi-workspace routing"),
):
    adapter = get_adapter()
    if not hasattr(adapter, '_client'):
        raise HTTPException(status_code=501, detail="File proxy not available in mock mode")

    from beever_atlas.infra.config import get_settings
    _settings = get_settings()
    bridge_url = f"{_settings.bridge_url}/bridge/files?url={url}"
    if connection_id:
        bridge_url += f"&connection_id={connection_id}"
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
