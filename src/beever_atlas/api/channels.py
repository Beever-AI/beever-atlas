"""Channel and message API endpoints."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from fastapi.responses import JSONResponse

from beever_atlas.adapters import ChannelInfo, get_adapter
from beever_atlas.adapters.bridge import BridgeError, ChatBridgeAdapter
from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import (
    assert_channel_access,
    assert_channel_delete_access,
)
from beever_atlas.infra.config import get_settings
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
    # Status of the parent PlatformConnection at the time this response
    # was built — ``"connected"`` / ``"disconnected"`` / ``"error"`` /
    # ``"pending"`` / ``None`` for orphan channels with no parent
    # connection. Lets the sidebar render a "disconnected — reconnect"
    # affordance instead of silently hiding the workspace when a
    # connection's status drifts away from ``"connected"``.
    connection_status: str | None = None


class WikiStateEntry(BaseModel):
    """Per-channel wiki readiness summary used by the sidebar / picker / home.

    Cheap to compute (one batched ``$in`` query against ``channel_sync_state``)
    and rendered everywhere a channel name appears, so we keep the payload
    minimal and the derivation rules in one place.
    """

    state: str  # "ready" | "empty"
    last_sync_ts: str | None = None
    total_synced_messages: int = 0


class WikiStatesResponse(BaseModel):
    """Map keyed by channel_id. Channels absent from the map are "empty"."""

    states: dict[str, WikiStateEntry]


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


def _channel_to_response(
    info: ChannelInfo,
    connection_status: str | None = None,
) -> ChannelResponse:
    return ChannelResponse(
        channel_id=info.channel_id,
        name=info.name,
        platform=info.platform,
        is_member=info.is_member,
        member_count=info.member_count,
        topic=info.topic,
        purpose=info.purpose,
        connection_id=info.connection_id,
        connection_status=connection_status,
    )


async def _synthesize_channels_from_selected(conn) -> list[ChannelInfo]:
    """Build ``ChannelInfo`` rows from a connection's ``selected_channels``
    pick-list when the live bridge fetch isn't usable (status != connected,
    bridge unreachable, token expired). Names are pulled from MongoDB's
    ``get_channel_display_name`` (last-known name from prior syncs); when
    that's also empty the channel id is used as the label so the sidebar
    still has something to render.

    The point: even when a workspace's connection is broken, the user
    should still see the channels they previously selected, grouped under
    the workspace label, so they know what they need to reconnect to
    recover. Silently hiding them sets the user up for the surprise the
    "Ungrouped tech-studio" report is about.
    """
    if not getattr(conn, "selected_channels", None):
        return []
    stores = get_stores()
    name_results = await asyncio.gather(
        *[stores.mongodb.get_channel_display_name(cid) for cid in conn.selected_channels],
        return_exceptions=True,
    )
    out: list[ChannelInfo] = []
    for cid, name in zip(conn.selected_channels, name_results):
        resolved_name = name if isinstance(name, str) and name else cid
        out.append(
            ChannelInfo(
                channel_id=cid,
                name=resolved_name,
                platform=conn.platform,
                is_member=True,  # user explicitly selected → treat as member
                connection_id=conn.id,
            )
        )
    return out


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


def _channel_message_row_to_response(row: dict[str, Any], channel_id: str) -> "MessageResponse":
    """Map a ``channel_messages`` row dict back to the API ``MessageResponse``.

    Used by the dual-read path when ``READ_FROM_MESSAGE_STORE`` is ON.
    Mirrors the field mapping the legacy adapter path applies:
    ``raw_metadata.is_bot`` and ``raw_metadata.links`` are surfaced as
    top-level fields, ``timestamp`` is rendered as ISO 8601, and the API
    derives ``platform`` from the row's ``source_id`` (for chat adapters
    this maps 1:1 to the platform name).

    ``channel_name`` is persisted on the row by the sync writer, so the
    response carries the platform's display name instead of falling back
    to the opaque ``channel_id``. The ``channel_id`` fallback remains for
    rows written before this field was added (back-compat).
    """
    raw_metadata = row.get("raw_metadata") or {}
    ts = row.get("timestamp")
    if isinstance(ts, datetime):
        ts_iso = ts.isoformat()
    else:
        ts_iso = str(ts) if ts else ""
    platform = row.get("source_id") or row.get("platform") or ""
    return MessageResponse(
        content=row.get("content", ""),
        author=row.get("author", ""),
        author_name=row.get("author_name", ""),
        author_image=row.get("author_image") or None,
        platform=str(platform),
        channel_id=row.get("channel_id", channel_id),
        channel_name=row.get("channel_name", channel_id),
        message_id=row.get("message_id", ""),
        timestamp=ts_iso,
        thread_id=row.get("thread_id"),
        attachments=row.get("attachments", []),
        reactions=row.get("reactions", []),
        reply_count=row.get("reply_count", 0),
        is_bot=bool(row.get("is_bot", raw_metadata.get("is_bot", False))),
        links=raw_metadata.get("links", []) or row.get("links", []),
    )


async def _compute_total_count(channel_id: str, adapter: Any | None) -> int | None:
    """Compute ``total_count`` identically across the store and adapter paths.

    Reads ``ChannelSyncState.total_synced_messages`` first; falls back to
    ``adapter.fetch_message_count`` when no sync state is available AND an
    adapter was supplied (the store-read path has no adapter to query). Keeps
    the response shape identical between dual-read branches.
    """
    total_count: int | None = None
    try:
        stores = get_stores()
        sync_state = await stores.mongodb.get_channel_sync_state(channel_id)
        if sync_state is not None and sync_state.total_synced_messages:
            total_count = sync_state.total_synced_messages
    except RuntimeError:
        pass
    if total_count is None and adapter is not None and hasattr(adapter, "fetch_message_count"):
        total_count = await adapter.fetch_message_count(channel_id)  # type: ignore[attr-defined]
    return total_count


async def _fetch_file_messages(
    channel_id: str,
    limit: int,
    since: str | None = None,
    order: str = "desc",
) -> "MessagesListResponse":
    """Read persisted messages for a file-imported channel.

    When ``READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES`` is ON AND
    ``channel_messages`` carries rows for this channel with
    ``source_id="file"``, the request is served from the unified Message
    Store. Otherwise falls back to the legacy ``imported_messages``
    collection. Mirrors the dual-read pattern for platform channels.
    """
    stores = get_stores()
    settings = get_settings()

    since_dt: datetime | None = None
    if since:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            since_dt = None

    if settings.read_file_imports_from_channel_messages:
        store_rows = await stores.mongodb.get_channel_messages(
            channel_id,
            limit=limit,
            since=since_dt,
            order=order,
            source_id="file",
        )
        if store_rows:
            logger.info(
                "file_imports_read",
                extra={
                    "event": "file_imports_read",
                    "channel_id": channel_id,
                    "row_count": len(store_rows),
                    "source": "channel_messages",
                },
            )
            response_messages = [
                _channel_message_row_to_response(row, channel_id) for row in store_rows
            ]
            total_count = await _compute_total_count(channel_id, adapter=None)
            return MessagesListResponse(
                messages=response_messages,
                total_count=total_count,
            )
        logger.info(
            "file_imports_fallback",
            extra={
                "event": "file_imports_fallback",
                "reason": "empty_store",
                "channel_id": channel_id,
            },
        )

    query: dict[str, Any] = {"channel_id": channel_id}
    if since_dt is not None:
        query["timestamp"] = {"$gte": since_dt}
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
    logger.info(
        "file_imports_read",
        extra={
            "event": "file_imports_read",
            "channel_id": channel_id,
            "row_count": len(messages),
            "source": "imported_messages",
        },
    )
    return MessagesListResponse(messages=messages, total_count=total)


@router.get("/api/channels", response_model=list[ChannelResponse])
async def list_channels() -> list[ChannelResponse]:
    """List channels from every platform connection — connected or not.

    Behaviour change (2026-05-06): previously this filtered by
    ``status == "connected"``, which silently hid all channels whose
    parent connection had drifted to ``disconnected`` / ``error`` /
    ``expired``. Channels that had been synced once survived via the
    orphan fallback below with ``connection_id=None`` and got grouped
    under "Ungrouped" in the sidebar; channels that hadn't been synced
    yet vanished entirely. The fix:

      * For ``status == "connected"``: live-fetch via the bridge as
        before so freshly-added channels in the platform are picked up
        on next refresh.
      * For non-connected statuses: synthesise ``ChannelInfo`` rows
        from ``conn.selected_channels`` directly (using last-known
        names from MongoDB) so the workspace label and pick-list
        survive a connection blip. ``connection_status`` on each row
        carries the truth so the sidebar can render a "disconnected —
        reconnect to sync" affordance next to the workspace label.
      * CSV-imported channels with sync state but no parent connection
        still surface via the orphan path with ``connection_id=None``
        and ``connection_status=None``.
    """
    from beever_atlas.stores import get_stores

    stores = get_stores()
    connections = await stores.platform.list_connections()

    # Map channel_id → connection_status so we can stamp each response
    # with its parent connection's state. ``None`` is reserved for
    # orphan channels (synced once, parent connection deleted).
    status_by_channel: dict[str, str] = {}
    all_channels: list[ChannelInfo] = []

    connected = [c for c in connections if c.status == "connected"]
    other = [c for c in connections if c.status != "connected"]

    # Live-fetch for connected workspaces (gives current channel
    # membership / topics / member counts).
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
                # Fall back to the persisted pick-list so the workspace
                # still appears even when its bridge is briefly down.
                #
                # Mark these channels as ``"error"`` regardless of the
                # row's stored ``conn.status`` — the live fetch just
                # failed, which is empirically more authoritative than
                # whatever the platform_store has cached. This is what
                # makes the sidebar's ⚠ badge show up for cases like
                # Slack ``account_inactive`` (token's underlying account
                # is dead but the connection row was never updated). The
                # persisted ``conn.status`` is a slow signal; bridge
                # fetch failure is a fast signal — prefer the fast one.
                synthesised = await _synthesize_channels_from_selected(conn)
                all_channels.extend(synthesised)
                for ch in synthesised:
                    status_by_channel[ch.channel_id] = "error"
                continue
            all_channels.extend(result)
            for ch in result:
                status_by_channel[ch.channel_id] = "connected"

    # Non-connected workspaces — synthesise from the pick-list so the
    # workspace label persists in the sidebar with a disconnected badge.
    for conn in other:
        synthesised = await _synthesize_channels_from_selected(conn)
        all_channels.extend(synthesised)
        for ch in synthesised:
            status_by_channel[ch.channel_id] = conn.status

    # Include CSV-imported channels (sync state exists but no connection)
    connected_channel_ids = {ch.channel_id for ch in all_channels}
    synced_ids = await stores.mongodb.list_synced_channel_ids()
    orphaned_ids = [cid for cid in synced_ids if cid not in connected_channel_ids]
    if orphaned_ids:
        name_results = await asyncio.gather(
            *[stores.mongodb.get_channel_display_name(cid) for cid in orphaned_ids]
        )
        for cid, name in zip(orphaned_ids, name_results):
            # RES-287/4a — orphaned channels (no connection_id) used to fall
            # back to "discord", which caused the sidebar to show the Discord
            # icon on Mattermost/Slack workspaces. "unknown" is the truthful
            # answer; PlatformIcon renders a neutral MessageSquare for it.
            platform = _detect_platform_from_channel_id(cid) or "unknown"
            all_channels.append(
                ChannelInfo(
                    channel_id=cid,
                    name=name or cid,
                    platform=platform,
                    is_member=True,
                    connection_id=None,
                )
            )

    responses = [
        _channel_to_response(ch, connection_status=status_by_channel.get(ch.channel_id))
        for ch in all_channels
    ]
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


@router.get("/api/channels/wiki-states", response_model=WikiStatesResponse)
async def list_channel_wiki_states() -> WikiStatesResponse:
    """Return per-channel wiki readiness for every channel with sync state.

    The sidebar, ask channel picker, and home page all need to render the
    same "this channel has a wiki / this one doesn't" signal next to a
    channel name. Doing that with N per-channel calls would be 50–200
    extra round trips on every page load; instead we resolve the universe
    of channels once and batch-fetch their sync state in a single
    ``$in`` query, mirroring the enrichment ``list_channels`` already
    performs for language metadata.

    State derivation (kept intentionally narrow — UI handles "building" /
    "errored" by overlaying live sync poll data from useSync):
      * ``"ready"``  — channel has a ChannelSyncState with at least one
        synced message (``total_synced_messages > 0``).
      * ``"empty"``  — no sync state, or zero synced messages. Channels
        absent from the map are treated as empty by the client too,
        which keeps the payload compact for fresh installs.
    """
    from beever_atlas.stores import get_stores

    stores = get_stores()

    # Universe of channels = everything in the channel_sync_state collection.
    # That captures both live-connected channels and CSV-imported / orphan
    # channels, which is exactly the set the sidebar / picker shows.
    try:
        synced_ids = await stores.mongodb.list_synced_channel_ids()
    except Exception:
        logger.debug("list_synced_channel_ids failed", exc_info=True)
        return WikiStatesResponse(states={})

    if not synced_ids:
        return WikiStatesResponse(states={})

    try:
        states_map = await stores.mongodb.get_channel_sync_states_batch(synced_ids)
    except Exception:
        logger.debug("get_channel_sync_states_batch failed", exc_info=True)
        return WikiStatesResponse(states={})

    out: dict[str, WikiStateEntry] = {}
    for cid, state in states_map.items():
        msgs = getattr(state, "total_synced_messages", 0) or 0
        out[cid] = WikiStateEntry(
            state="ready" if msgs > 0 else "empty",
            last_sync_ts=getattr(state, "last_sync_ts", None),
            total_synced_messages=msgs,
        )
    return WikiStatesResponse(states=out)


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
        # RES-287/4a — see list_channels above; same orphan-platform fallback.
        platform = _detect_platform_from_channel_id(channel_id) or "unknown"
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

    since_dt = None
    if since:
        since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))

    # Dual-read fallback during migration. When the READ_FROM_MESSAGE_STORE
    # flag is ON, prefer the durable ``channel_messages`` collection populated
    # by the sync runner and fall back to ``adapter.fetch_history`` when
    # (a) the store has zero rows for this channel, or (b) a sync is currently
    # writing into it (status="running") — in either case the user might
    # otherwise see partial data. See
    # ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/message-store/``
    # → "Dual-read fallback during migration".
    if get_settings().read_from_message_store:
        store_rows = await stores.mongodb.get_channel_messages(
            channel_id,
            limit=limit,
            since=since_dt,
            before=before,
            order=order,
        )
        sync_job = None
        try:
            sync_job = await stores.mongodb.get_sync_status(channel_id)
        except Exception:
            logger.debug(
                "Failed to fetch sync status for channel %s during dual-read",
                channel_id,
                exc_info=True,
            )
        sync_running = sync_job is not None and sync_job.status == "running"

        if store_rows and not sync_running:
            logger.info(
                "channel_messages_read",
                extra={
                    "event": "channel_messages_read",
                    "channel_id": channel_id,
                    "row_count": len(store_rows),
                },
            )
            response_messages = [
                _channel_message_row_to_response(row, channel_id) for row in store_rows
            ]
            total_count = await _compute_total_count(channel_id, adapter=None)
            return MessagesListResponse(
                messages=response_messages,
                total_count=total_count,
            )

        fallback_reason = "sync_in_progress" if sync_running else "empty_store"
        logger.info(
            "channel_messages_fallback",
            extra={
                "event": "channel_messages_fallback",
                "reason": fallback_reason,
                "channel_id": channel_id,
            },
        )

    adapter = await _resolve_adapter_for_channel(channel_id, connection_id)

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
    total_count = await _compute_total_count(channel_id, adapter=adapter)
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


_FAILURE_LAST_ERROR_TRUNCATE = 500


def _strip_traceback(raw: str | None) -> str:
    """Drop Python traceback bodies from a stored exception message.

    The worker stores ``str(exc)`` (often just the exception class +
    message), but if a layer above it stored ``traceback.format_exc()``
    instead, we MUST not echo the raw stack to operators — too easy to
    leak file paths or library internals into a UI.
    """
    if not raw:
        return ""
    text = raw
    # Common Python traceback marker — keep only the final ``ExcClass: msg`` line.
    if "Traceback (most recent call last):" in text:
        # Take everything after the last newline (last frame is usually
        # ``ExcClass: msg``); fall back to the whole string if no newline.
        text = text.rstrip().split("\n")[-1].strip()
    return text[:_FAILURE_LAST_ERROR_TRUNCATE]


@router.get("/api/channels/{channel_id}/extraction-failures")
async def get_channel_extraction_failures(
    channel_id: str,
    cursor: str | None = None,
    limit: int = 50,
    principal: Principal = Depends(require_user),
) -> dict[str, Any]:
    """Paginated list of ``channel_messages`` rows stuck in ``extraction_status="failed"``.

    Backs the wiki UI's ``FailedBatchPanel`` drill-down. Each row carries
    ``message_id``, ``next_attempt_at``, ``attempt_count``, and a
    server-truncated ``last_error`` (max 500 chars, stack traces stripped).
    Cursor-paginated; ``next_cursor`` is non-null when more rows remain.
    Default page size 50, capped at 200.
    """
    from beever_atlas.stores import get_stores

    await assert_channel_access(principal, channel_id)
    safe_limit = max(1, min(int(limit), 200))
    stores = get_stores()
    rows, next_cursor = await stores.mongodb.list_failed_channel_messages(
        channel_id, cursor=cursor, limit=safe_limit
    )
    items: list[dict[str, Any]] = []
    for row in rows:
        next_attempt = row.get("next_attempt_at")
        if hasattr(next_attempt, "isoformat"):
            next_attempt = next_attempt.isoformat()
        items.append(
            {
                "message_id": row.get("message_id"),
                "next_attempt_at": next_attempt,
                "attempt_count": int(row.get("attempt_count", 0) or 0),
                "last_error": _strip_traceback(row.get("last_error")),
            }
        )
    return {"items": items, "next_cursor": next_cursor}


@router.get("/api/channels/{channel_id}/extraction-status")
async def get_channel_extraction_status(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> dict[str, Any]:
    """Return per-status extraction counts for a channel.

    Backs the frontend's "Enriching: X of Y messages complete" progress
    row shown when ``DECOUPLE_EXTRACTION`` is ON. Counts are aggregated
    via a single MongoDB pipeline that hits the partial-filter index
    on ``(extraction_status, next_attempt_at)``.

    Response shape::

        {
            "channel_id": "...",
            "counts": {"pending": N, "extracting": N, "done": N, "failed": N},
            "total": N
        }

    Always zero-fills missing statuses so consumers can render a stable
    progress bar without status-keyed conditionals.
    """
    from beever_atlas.stores import get_stores

    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    counts = await stores.mongodb.count_channel_messages_by_status(channel_id)
    total = sum(counts.values())
    return {
        "channel_id": channel_id,
        "counts": counts,
        "total": total,
    }


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


async def _channel_is_referenced_anywhere(channel_id: str) -> bool:
    """Return True if ``channel_id`` is known to the deployment at all.

    A channel is a valid hard-delete target when it is referenced by any
    connection's ``selected_channels`` (sync pick-list) OR has synced data
    (``list_synced_channel_ids`` — the same orphan resolution the GET
    ``/api/channels/{id}`` handler uses for direct-URL navigation). Orphans
    that still hold data are valid delete targets; only a genuinely-unknown
    channel (referenced nowhere) is a 404.
    """
    stores = get_stores()
    connections = await stores.platform.list_connections()
    for conn in connections:
        if channel_id in (getattr(conn, "selected_channels", None) or []):
            return True
    synced_ids = await stores.mongodb.list_synced_channel_ids()
    return channel_id in set(synced_ids)


@router.delete("/api/channels/{channel_id}")
async def delete_channel(
    channel_id: str,
    confirm: str = Query(
        ...,
        description=(
            "Type-to-confirm guard. Must equal the channel's display name "
            "(what the UI shows); the raw channel_id is accepted only as a "
            "fallback when no display name is stored."
        ),
    ),
    principal: Principal = Depends(require_user),
):
    """Hard-purge a channel from every store (delete-channel-v2 Wave 3).

    This is the DESTRUCTIVE full delete — distinct from
    ``DELETE /api/channels/{channel_id}/data`` (which only resets derived
    data and keeps the channel addressable). It unlinks the channel from
    every connection, de-registers its scheduler timers, and deletes all
    Mongo / Weaviate / graph / wiki / chat data behind an atomically-claimed
    purge lock (see :func:`beever_atlas.services.channel_deletion.purge_channel`).

    Order of checks (authz BEFORE everything else):

      1. ``assert_channel_delete_access`` — tenancy-aware destructive authz.
         Stricter than the read path: no orphan-permissive fallback.
      2. 404 when the channel is referenced nowhere (no connection pick-list,
         no synced data) — read-only, no lock claimed. A purged channel that
         lingers in the grid only because it's a live channel on the connected
         platform is "already gone" → 404, not a confusing confirm-mismatch
         400. Orphans with data are still valid targets.
      3. ``confirm`` must match the channel display name (the channel_id is
         accepted only as a fallback when no display name is stored); a
         mismatch is a 400 and NO store is touched (the lock is never
         claimed). This is anti-accidental UI friction, not an authz control.

    Status → HTTP mapping (the service never raises for a missing channel
    and never 500s — per-store failures are isolated into ``errors``):

      * ``"completed"`` (``errors == {}``) → 200 with the full result body.
      * ``"partial"``   (``errors != {}``) → 207 with the full result body
        (incl. ``errors``); the lock is retained and the reaper converges.
      * ``"already_in_progress"`` (CAS loser) → 200 with ``{channel_id,
        status}`` only (NO ``counts``/``errors``) plus a message.
    """
    from beever_atlas.services.channel_deletion import purge_channel

    # 1. Destructive authz FIRST — before confirm validation or any lookup.
    await assert_channel_delete_access(principal, channel_id)

    stores = get_stores()

    # 2. Existence check FIRST. A channel referenced nowhere — no connection
    #    pick-list entry and no synced data — is already gone. This notably
    #    covers a previously-purged channel that still shows in the grid only
    #    because it's a live channel on the connected platform: after a purge
    #    the stored display name is gone, so the confirm guard below would
    #    otherwise reject the exact name the user still sees with a confusing
    #    400. Returning 404 here is honest ("already deleted") and lets the UI
    #    drop the card. Read-only — no lock claimed.
    if not await _channel_is_referenced_anywhere(channel_id):
        raise HTTPException(status_code=404, detail=f"Channel {channel_id} not found")

    # 3. Type-to-confirm guard. ``confirm`` is anti-accidental UI friction
    #    (make the user type what they see) — NOT an authz control; the real
    #    destructive authz is ``assert_channel_delete_access`` above. The
    #    channel display name is not a secret. When a display name is stored we
    #    accept ONLY that (trimmed) — the UI sends exactly what the user sees,
    #    so also accepting the raw id is a needless second key that widens the
    #    accepted set. The raw ``channel_id`` is accepted ONLY as a fallback
    #    when no display name exists (orphans the UI labels by id). Validate
    #    AFTER the existence check but BEFORE touching any store (no lock
    #    claimed on mismatch). NOTE: ``confirm`` stays a query param on purpose
    #    — the shared ``web/src/lib/api.ts`` ``delete`` wrapper has no
    #    request-body support and DELETE-with-body is fragile across proxies.
    display_name = await stores.mongodb.get_channel_display_name(channel_id)
    if display_name and display_name.strip():
        accepted = {display_name.strip()}
    else:
        accepted = {channel_id}
    if confirm not in accepted:
        raise HTTPException(
            status_code=400,
            detail=(
                "confirm does not match the channel name; "
                "type the channel name exactly to confirm deletion"
            ),
        )

    result = await purge_channel(channel_id, principal_id=principal.id)
    status = result.get("status")

    if status == "already_in_progress":
        # CAS loser — another purge (re-click or reaper) already holds the
        # lock. The body has ONLY {channel_id, status}; do NOT assume counts.
        return {
            **result,
            "message": "A delete for this channel is already in progress.",
        }

    if status == "partial":
        # At least one store failed; the lock is retained and the reaper
        # will converge. Surface 207 so the frontend can warn instead of
        # treating the channel as fully gone.
        return JSONResponse(status_code=207, content=result)

    # "completed" — clean run, 200 with the full body.
    return result


# `proxy_file` was relocated to `beever_atlas.api.loaders` (issue #88) so it
# can be mounted with `require_user_loader` (accepts ?access_token=) while
# the rest of this router stays header-only via `require_user`.
