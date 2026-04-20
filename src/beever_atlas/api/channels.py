"""Channel and message API endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

import httpx as _httpx

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from beever_atlas.adapters import ChannelInfo, get_adapter
from beever_atlas.adapters.bridge import BridgeError, ChatBridgeAdapter
from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.services.channel_discovery import (
    fetch_connection_channels,
    make_bridge_adapter,
)
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


def _get_adapter_for_connection(connection_id: str | None = None):
    """Return a connection-scoped adapter, honoring ADAPTER_MOCK=true.

    In mock mode we always use the singleton MockAdapter regardless of
    connection_id (mock has no notion of distinct workspaces). This lets
    integration tests drive the channels API without a real bridge.
    """
    import os

    if os.environ.get("ADAPTER_MOCK", "").lower() in ("true", "1", "yes"):
        return get_adapter()
    if connection_id:
        return ChatBridgeAdapter(connection_id=connection_id)
    base = get_adapter()
    if isinstance(base, ChatBridgeAdapter):
        return base
    return ChatBridgeAdapter()


async def _resolve_adapter_for_channel(channel_id: str, connection_id: str | None = None):
    """Resolve the correct adapter for a channel, with multi-workspace fallback.

    Tries the explicit connection_id first. If that fails (wrong workspace),
    searches all connections to find the one that owns this channel.
    Honors ADAPTER_MOCK=true via `make_bridge_adapter`.
    """
    if connection_id:
        adapter = make_bridge_adapter(connection_id)
        try:
            await adapter.get_channel_info(channel_id)
            return adapter
        except Exception:
            await adapter.close()
            # Fall through to search

    from beever_atlas.stores import get_stores

    stores = get_stores()
    connections = await stores.platform.list_connections()
    connected = [c for c in connections if c.status == "connected"]

    likely_platform = _detect_platform_from_channel_id(channel_id)
    candidates = (
        ([c for c in connected if c.platform == likely_platform] or connected)
        if likely_platform
        else connected
    )

    for conn in candidates:
        if conn.id == connection_id:
            continue  # Already tried this one
        adapter = make_bridge_adapter(conn.id)
        try:
            await adapter.get_channel_info(channel_id)
            return adapter
        except Exception:
            await adapter.close()
            continue

    # Last resort: return default adapter
    return _get_adapter_for_connection(connection_id)


class ChannelResponse(BaseModel):
    channel_id: str
    name: str
    platform: str
    is_member: bool = False
    member_count: int | None = None
    topic: str | None = None
    purpose: str | None = None
    connection_id: str | None = None
    primary_language: str | None = None
    primary_language_confidence: float | None = None


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


async def _enrich_with_language(resp: ChannelResponse) -> ChannelResponse:
    """Populate primary_language fields from ChannelSyncState; swallow all errors."""
    try:
        stores = get_stores()
        state = await stores.mongodb.get_channel_sync_state(resp.channel_id)
        if state is not None:
            lang = state.primary_language
            conf = state.primary_language_confidence
            resp = resp.model_copy(
                update={
                    "primary_language": lang if lang else None,
                    "primary_language_confidence": conf if conf is not None else None,
                }
            )
    except Exception:
        logger.debug(
            "Failed to enrich channel %s with language metadata",
            resp.channel_id,
            exc_info=True,
        )
    return resp


def _apply_language_state(resp: ChannelResponse, state: Any | None) -> ChannelResponse:
    """In-memory variant of _enrich_with_language using a pre-fetched state."""
    if state is None:
        return resp
    lang = getattr(state, "primary_language", None)
    conf = getattr(state, "primary_language_confidence", None)
    return resp.model_copy(
        update={
            "primary_language": lang if lang else None,
            "primary_language_confidence": conf if conf is not None else None,
        }
    )


async def _fetch_file_messages(
    channel_id: str,
    limit: int,
    since: str | None = None,
    order: str = "desc",
) -> "MessagesListResponse":
    """Read persisted messages for a file-imported channel."""
    stores = get_stores()
    query: dict[str, Any] = {"channel_id": channel_id}
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            query["timestamp"] = {"$gte": since_dt}
        except ValueError:
            pass
    sort_dir = -1 if order == "desc" else 1
    cursor = (
        stores.mongodb.db["imported_messages"].find(query).sort("timestamp", sort_dir).limit(limit)
    )
    messages: list[MessageResponse] = []
    async for doc in cursor:
        ts = doc.get("timestamp")
        ts_iso = doc.get("timestamp_iso") or (
            ts.isoformat() if isinstance(ts, datetime) else str(ts) if ts else ""
        )
        messages.append(
            MessageResponse(
                content=doc.get("content", ""),
                author=doc.get("author", ""),
                author_name=doc.get("author_name", ""),
                author_image=doc.get("author_image") or None,
                platform="file",
                channel_id=channel_id,
                channel_name=doc.get("channel_name", channel_id),
                message_id=doc.get("message_id", ""),
                timestamp=ts_iso,
                thread_id=doc.get("thread_id"),
                attachments=doc.get("attachments", []),
                reactions=doc.get("reactions", []),
                reply_count=doc.get("reply_count", 0),
                is_bot=False,
                links=[],
            )
        )
    total = await stores.mongodb.db["imported_messages"].count_documents({"channel_id": channel_id})
    return MessagesListResponse(messages=messages, total_count=total)


@router.get("/api/channels", response_model=list[ChannelResponse])
async def list_channels() -> list[ChannelResponse]:
    """List channels from all connected platform connections.

    Iterates every PlatformConnection with status='connected', fetches
    channels per-connection in parallel, and filters by each connection's
    selected_channels list.  One failing connection does not block the others.

    Also includes channels that were imported via CSV (have sync state in
    MongoDB but no platform connection), so they appear in the sidebar.
    """
    from beever_atlas.stores import get_stores

    stores = get_stores()
    connections = await stores.platform.list_connections()
    connected = [c for c in connections if c.status == "connected"]

    all_channels: list[ChannelInfo] = []

    if connected:
        tasks = [
            fetch_connection_channels(conn.id, conn.selected_channels, conn.platform)
            for conn in connected
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for conn, result in zip(connected, results):
            if isinstance(result, BaseException):
                logger.warning(
                    "Failed to fetch channels for connection %s (%s): %s",
                    conn.id,
                    conn.display_name,
                    result,
                )
                continue
            all_channels.extend(result)

    # Include CSV-imported channels (sync state exists but no connection)
    connected_channel_ids = {ch.channel_id for ch in all_channels}
    synced_ids = await stores.mongodb.list_synced_channel_ids()
    orphaned_ids = [cid for cid in synced_ids if cid not in connected_channel_ids]
    if orphaned_ids:
        name_results = await asyncio.gather(
            *[stores.mongodb.get_channel_display_name(cid) for cid in orphaned_ids]
        )
        for cid, name in zip(orphaned_ids, name_results):
            platform = _detect_platform_from_channel_id(cid) or "discord"
            all_channels.append(
                ChannelInfo(
                    channel_id=cid,
                    name=name or cid,
                    platform=platform,
                    is_member=True,
                    connection_id=None,
                )
            )

    responses = [_channel_to_response(ch) for ch in all_channels]
    # Batch enrich: single $in query instead of N per-channel reads.
    try:
        states_map = await stores.mongodb.get_channel_sync_states_batch(
            [r.channel_id for r in responses]
        )
    except Exception:
        logger.debug("Failed to batch-fetch channel sync states", exc_info=True)
        states_map = {}
    responses = [_apply_language_state(r, states_map.get(r.channel_id)) for r in responses]
    return list(responses)


@router.get("/api/channels/{channel_id}", response_model=ChannelResponse)
async def get_channel(
    channel_id: str,
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> ChannelResponse:
    """Get metadata for a specific channel.

    When *connection_id* is provided, fetches directly from that connection.
    Otherwise, iterates all connected PlatformConnections until the channel is
    found — this supports direct URL navigation and page refreshes where no
    route state (and therefore no connection_id) is available.
    """
    await assert_channel_access(principal, channel_id)
    if connection_id:
        adapter = make_bridge_adapter(connection_id)
        try:
            info = await adapter.get_channel_info(channel_id)
            return await _enrich_with_language(_channel_to_response(info))
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
        adapter = make_bridge_adapter(conn.id)
        try:
            info = await adapter.get_channel_info(channel_id)
            return await _enrich_with_language(_channel_to_response(info))
        except (KeyError, BridgeError):
            continue
        except Exception:
            continue
        finally:
            await adapter.close()

    # Fallback: check if this is a file-imported channel (tied to the
    # file connection's selected_channels) or a legacy CSV sync-state entry.
    file_conn = next((c for c in connected if c.platform == "file"), None)
    if file_conn is not None and channel_id in file_conn.selected_channels:
        name = await stores.mongodb.get_channel_display_name(channel_id)
        return await _enrich_with_language(
            ChannelResponse(
                channel_id=channel_id,
                name=name or channel_id,
                platform="file",
                is_member=True,
                connection_id=file_conn.id,
            )
        )

    synced_ids = await stores.mongodb.list_synced_channel_ids()
    if channel_id in synced_ids:
        name = await stores.mongodb.get_channel_display_name(channel_id)
        platform = _detect_platform_from_channel_id(channel_id) or "discord"
        return await _enrich_with_language(
            ChannelResponse(
                channel_id=channel_id,
                name=name or channel_id,
                platform=platform,
                is_member=True,
                connection_id=None,
            )
        )

    raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")


@router.get("/api/channels/{channel_id}/messages", response_model=MessagesListResponse)
async def get_channel_messages(
    channel_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    since: str | None = Query(default=None, description="ISO 8601 datetime filter"),
    before: str | None = Query(
        default=None, description="Message ID cursor - fetch messages before this ID"
    ),
    order: str = Query(
        default="desc", description="Sort order: desc (newest first) or asc (oldest first)"
    ),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> MessagesListResponse:
    """Get paginated messages for a channel."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()

    # File-imported channels: read from the imported_messages collection
    # instead of calling the bridge (there is no upstream).
    connections = await stores.platform.list_connections()
    file_conn = next(
        (c for c in connections if c.platform == "file" and c.status == "connected"),
        None,
    )
    is_file_channel = (file_conn is not None and channel_id in file_conn.selected_channels) or (
        connection_id is not None and file_conn is not None and connection_id == file_conn.id
    )
    if is_file_channel:
        return await _fetch_file_messages(channel_id, limit=limit, since=since, order=order)

    # CSV-imported channels have no live bridge connection — detect by ID format.
    # Real platform channels always have a recognisable ID (e.g. Slack C…, Discord snowflake).
    # CSV-imported channels use arbitrary IDs (e.g. "example_chat") that don't match any platform.
    if _detect_platform_from_channel_id(channel_id) is None and not connection_id:
        synced_ids = await stores.mongodb.list_synced_channel_ids()
        if channel_id in synced_ids:
            sync_state = await stores.mongodb.get_channel_sync_state(channel_id)
            total = sync_state.total_synced_messages if sync_state else None
            return MessagesListResponse(messages=[], total_count=total)

    adapter = await _resolve_adapter_for_channel(channel_id, connection_id)

    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))

    try:
        messages = await adapter.fetch_history(
            channel_id, since=since_dt, limit=limit, before=before, order=order
        )
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
        total_count = await adapter.fetch_message_count(channel_id)  # type: ignore[attr-defined]
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
    principal: Principal = Depends(require_user),
) -> list[MessageResponse]:
    """Get all messages in a thread (parent + replies)."""
    await assert_channel_access(principal, channel_id)
    adapter = await _resolve_adapter_for_channel(channel_id, connection_id)
    try:
        messages = await adapter.fetch_thread(channel_id, thread_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Thread {thread_id} not found") from e
    except BridgeError as e:
        raise HTTPException(status_code=e.status_code or 502, detail=str(e)) from e

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
async def clear_channel_data(
    channel_id: str,
    principal: Principal = Depends(require_user),
):
    """Delete all synced data (facts, entities, events, media, sync state) for a channel."""
    from beever_atlas.stores import get_stores

    await assert_channel_access(principal, channel_id)
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
    connection_id: str | None = Query(
        None, description="Connection ID for multi-workspace routing"
    ),
):
    adapter = get_adapter()
    if not hasattr(adapter, "_client"):
        raise HTTPException(status_code=501, detail="File proxy not available in mock mode")

    from beever_atlas.infra.config import get_settings
    from beever_atlas.infra.http_safe import validate_proxy_url
    from urllib.parse import quote, urlparse

    try:
        encoded_url = validate_proxy_url(url)
    except (PermissionError, ValueError) as exc:
        # Surface a generic message; never echo the attacker-controlled URL.
        # Log the host only so operators can see legitimate misses.
        host = urlparse(url).hostname if url else None
        logger.warning("file_proxy rejected url: host=%s reason=%s", host, type(exc).__name__)
        raise HTTPException(status_code=400, detail="Invalid file URL") from None

    _settings = get_settings()
    bridge_url = f"{_settings.bridge_url}/bridge/files?url={encoded_url}"
    if connection_id:
        bridge_url += f"&connection_id={quote(connection_id, safe='')}"
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
