"""Platform connection management API endpoints."""

from __future__ import annotations

import hmac
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.config import get_settings
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateConnectionRequest(BaseModel):
    platform: str
    display_name: str = ""
    credentials: dict[str, str]


class UpdateChannelsRequest(BaseModel):
    selected_channels: list[str]


class ConnectionResponse(BaseModel):
    """Public representation — credentials always redacted."""

    id: str
    platform: str
    display_name: str
    selected_channels: list[str]
    status: str
    error_message: str | None
    source: str
    created_at: str
    updated_at: str


class ChannelItem(BaseModel):
    channel_id: str
    name: str
    is_member: bool = False
    member_count: int | None = None
    topic: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_response(conn) -> ConnectionResponse:
    return ConnectionResponse(
        id=conn.id,
        platform=conn.platform,
        display_name=conn.display_name,
        selected_channels=conn.selected_channels,
        status=conn.status,
        error_message=conn.error_message,
        source=conn.source,
        created_at=conn.created_at.isoformat(),
        updated_at=conn.updated_at.isoformat(),
    )


def _bridge_client() -> httpx.AsyncClient:
    """Return an httpx client pre-configured for the bridge service."""
    settings = get_settings()
    headers: dict[str, str] = {}
    if settings.bridge_api_key:
        headers["Authorization"] = f"Bearer {settings.bridge_api_key}"
    return httpx.AsyncClient(
        base_url=settings.bridge_url,
        headers=headers,
        timeout=30.0,
    )


async def _register_adapter(
    platform: str, credentials: dict, connection_id: str | None = None,
) -> None:
    """Call POST /bridge/adapters to register the adapter in the bot service.

    Raises HTTPException with a user-friendly message on failure.
    """
    body: dict[str, Any] = {"platform": platform, "credentials": credentials}
    if connection_id:
        body["connectionId"] = connection_id

    async with _bridge_client() as client:
        try:
            resp = await client.post("/bridge/adapters", json=body)
        except httpx.ConnectError as e:
            raise HTTPException(
                status_code=503,
                detail="Bot service is unreachable. Check that the bot is running.",
            ) from e

        if resp.status_code != 200:
            data: dict[str, Any] = {}
            try:
                data = resp.json()
            except Exception:
                pass
            raise HTTPException(
                status_code=400,
                detail=data.get("message", f"Adapter registration failed: {resp.status_code}"),
            )


async def _unregister_adapter(connection_id: str) -> None:
    """Call DELETE /bridge/adapters/{connectionId} to remove the adapter.

    Errors are logged but not re-raised — unregister is best-effort during rollback.
    """
    async with _bridge_client() as client:
        try:
            resp = await client.delete(f"/bridge/adapters/{connection_id}")
            if resp.status_code not in (200, 404):
                logger.warning(
                    "Bridge unregister returned %d for connection %s",
                    resp.status_code,
                    connection_id,
                )
        except httpx.ConnectError:
            logger.warning("Bridge unreachable during adapter rollback for connection %s", connection_id)


async def _list_bridge_channels(
    platform: str, connection_id: str | None = None,
) -> list[dict[str, Any]]:
    """List channels via the bridge.

    When connection_id is provided, uses the connection-scoped route.
    Otherwise falls back to the platform-aggregated route.
    """
    if connection_id:
        path = f"/bridge/connections/{connection_id}/channels"
    else:
        path = f"/bridge/platforms/{platform}/channels"

    async with _bridge_client() as client:
        try:
            resp = await client.get(path)
        except httpx.ConnectError as e:
            raise HTTPException(
                status_code=503,
                detail="Bot service is unreachable when listing channels.",
            ) from e

        if resp.status_code != 200:
            data: dict[str, Any] = {}
            try:
                data = resp.json()
            except Exception:
                pass
            raise HTTPException(
                status_code=502,
                detail=data.get("message", f"Channel listing failed: {resp.status_code}"),
            )

        payload = resp.json()
        return payload.get("channels", [])


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/api/connections", response_model=list[ConnectionResponse])
async def list_connections() -> list[ConnectionResponse]:
    """List all platform connections (credentials redacted)."""
    stores = get_stores()
    connections = await stores.platform.list_connections()
    return [_to_response(c) for c in connections]


class _InternalConnectionItem(BaseModel):
    connection_id: str
    platform: str
    credentials: dict[str, str]
    status: str


@router.get("/api/internal/connections/credentials")
async def list_connections_with_credentials(request: Request) -> list[_InternalConnectionItem]:
    """Internal endpoint for bot startup sync — returns decrypted credentials.

    Secured by BRIDGE_API_KEY header. Never expose to end users.
    """
    settings = get_settings()
    expected_key = settings.bridge_api_key
    if not expected_key:
        raise HTTPException(status_code=403, detail="BRIDGE_API_KEY not configured")

    auth = request.headers.get("authorization", "")
    expected = f"Bearer {expected_key}"
    if not hmac.compare_digest(auth, expected):
        if getattr(settings, "bridge_hmac_dual", False) and auth == expected:
            logger.warning(
                "bridge auth accepted via legacy == path (BEEVER_BRIDGE_HMAC_DUAL); "
                "retire this flag after the next release"
            )
        else:
            raise HTTPException(status_code=401, detail="Unauthorized")

    stores = get_stores()
    connections = await stores.platform.list_connections()
    result: list[_InternalConnectionItem] = []
    for conn in connections:
        if conn.status != "connected":
            continue
        try:
            creds = stores.platform.decrypt_connection_credentials(conn)
            result.append(_InternalConnectionItem(
                connection_id=conn.id,
                platform=conn.platform,
                credentials=creds,
                status=conn.status,
            ))
        except Exception as e:
            logger.warning("Failed to decrypt credentials for connection %s: %s", conn.id, e)
    return result


@router.post("/api/connections", response_model=ConnectionResponse, status_code=201)
async def create_connection(
    body: CreateConnectionRequest,
    principal: Principal = Depends(require_user),
) -> ConnectionResponse:
    """Create a new platform connection.

    Flow:
    1. Generate connection ID up-front (used for adapter registration and rollback).
    2. Register adapter in bot service (POST /bridge/adapters).
    3. Verify channel access via connection-scoped route.
    4. Encrypt credentials and persist to MongoDB.

    On failure, rollback the adapter registration using the same connection ID.
    """
    import uuid

    stores = get_stores()
    platform = body.platform.lower()

    if platform not in ("slack", "discord", "teams", "telegram", "mattermost", "file"):
        raise HTTPException(status_code=400, detail=f"Unsupported platform: {platform!r}")

    # "file" connections are created by POST /api/imports/commit with no
    # remote credentials. They skip bridge registration entirely.
    if platform == "file":
        raise HTTPException(
            status_code=400,
            detail=(
                "File connections are created via POST /api/imports/commit, "
                "not the generic connections endpoint."
            ),
        )

    if not body.display_name.strip():
        raise HTTPException(
            status_code=422,
            detail="display_name is required. Provide a name to identify this connection.",
        )

    # Duplicate check: reject if another connection already uses the same token
    existing = await stores.platform.list_connections()
    new_token = body.credentials.get("bot_token", "")
    for existing_conn in existing:
        if existing_conn.status != "connected" or existing_conn.platform != platform:
            continue
        try:
            existing_creds = stores.platform.decrypt_connection_credentials(existing_conn)
            if existing_creds.get("bot_token") == new_token and new_token:
                raise HTTPException(
                    status_code=409,
                    detail=f'This workspace is already connected as "{existing_conn.display_name}".',
                )
        except HTTPException:
            raise
        except Exception:
            pass  # skip connections we can't decrypt

    # Generate ID before registration so rollback can target the right adapter
    connection_id = str(uuid.uuid4())

    # Step 1: register adapter — raises HTTPException on failure
    await _register_adapter(platform, body.credentials, connection_id=connection_id)

    # Step 2: verify channel access — skipped for webhook-driven platforms (Telegram, Teams)
    # that have no channel listing API. They receive messages via webhook only.
    _WEBHOOK_ONLY_PLATFORMS = {"telegram", "teams"}
    if platform not in _WEBHOOK_ONLY_PLATFORMS:
        try:
            await _list_bridge_channels(platform, connection_id=connection_id)
        except HTTPException:
            await _unregister_adapter(connection_id)
            raise

    # Step 3: persist encrypted connection.
    # `owner_principal_id` is stamped with the authenticated caller's principal
    # id (RES-177 H1) so `_assert_channel_access` can gate downstream routes.
    owner_id = getattr(principal, "id", None) or str(principal)
    conn = await stores.platform.create_connection(
        platform=platform,
        display_name=body.display_name.strip(),
        credentials=body.credentials,
        status="connected",
        source="ui",
        connection_id=connection_id,
        owner_principal_id=owner_id,
    )

    logger.info("Created platform connection id=%s platform=%s", conn.id, conn.platform)
    return _to_response(conn)


@router.delete("/api/connections/{connection_id}", status_code=204)
async def delete_connection(connection_id: str) -> None:
    """Disconnect and remove a platform connection."""
    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    # Unregister from bot service (best-effort — don't fail if bot is down)
    await _unregister_adapter(conn.id)

    deleted = await stores.platform.delete_connection(connection_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    logger.info("Deleted platform connection id=%s platform=%s", connection_id, conn.platform)


@router.post("/api/connections/{connection_id}/validate", response_model=ConnectionResponse)
async def validate_connection(connection_id: str) -> ConnectionResponse:
    """Re-validate an existing connection by testing the adapter."""
    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    _WEBHOOK_ONLY_PLATFORMS = {"telegram", "teams"}
    try:
        credentials = stores.platform.decrypt_connection_credentials(conn)
        await _register_adapter(conn.platform, credentials, connection_id=conn.id)
        if conn.platform not in _WEBHOOK_ONLY_PLATFORMS:
            await _list_bridge_channels(conn.platform, connection_id=conn.id)
        updated = await stores.platform.update_connection(
            connection_id,
            status="connected",
            error_message=None,
        )
    except HTTPException as e:
        updated = await stores.platform.update_connection(
            connection_id,
            status="error",
            error_message=e.detail,
        )
        if updated is None:
            raise
        return _to_response(updated)

    if updated is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")
    return _to_response(updated)


@router.get("/api/connections/{connection_id}/channels", response_model=list[ChannelItem])
async def list_connection_channels(connection_id: str) -> list[ChannelItem]:
    """List available channels for a platform connection."""
    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    # Teams has no channel-listing path wired up (Graph API not implemented) — short-circuit
    # to avoid a 501. Telegram falls through: the bridge returns an in-memory registry of
    # chats the bot has received updates from (populated on webhook delivery).
    if conn.platform == "teams":
        return []

    raw_channels = await _list_bridge_channels(conn.platform, connection_id=conn.id)
    # Only return channels where the bot is a member — the bot can only
    # read messages from channels it's been invited to.
    return [
        ChannelItem(
            channel_id=ch.get("channel_id", ""),
            name=ch.get("name", ""),
            is_member=ch.get("is_member", False),
            member_count=ch.get("member_count"),
            topic=ch.get("topic"),
        )
        for ch in raw_channels
        if ch.get("is_member", False)
    ]


@router.put("/api/connections/{connection_id}/channels", response_model=ConnectionResponse)
async def update_selected_channels(
    connection_id: str,
    body: UpdateChannelsRequest,
) -> ConnectionResponse:
    """Update the selected channels for a connection and trigger sync."""
    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    updated = await stores.platform.update_connection(
        connection_id,
        selected_channels=body.selected_channels,
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    # Trigger sync for newly selected channels
    new_channels = set(body.selected_channels) - set(conn.selected_channels)
    if new_channels:
        await _trigger_sync_for_channels(list(new_channels), connection_id=connection_id)

    logger.info(
        "Updated channels for connection id=%s: %d selected, %d new",
        connection_id,
        len(body.selected_channels),
        len(new_channels),
    )
    return _to_response(updated)


async def _trigger_sync_for_channels(channel_ids: list[str], connection_id: str | None = None) -> None:
    """Fire-and-forget sync for newly selected channels."""
    from beever_atlas.api.sync import get_sync_runner

    runner = get_sync_runner()
    for channel_id in channel_ids:
        try:
            await runner.start_sync(channel_id, sync_type="full", connection_id=connection_id)
            logger.info("Triggered sync for newly selected channel %s", channel_id)
        except ValueError:
            # Sync already running — that's fine
            logger.debug("Sync already running for channel %s, skipping", channel_id)
        except Exception as e:
            logger.warning("Failed to trigger sync for channel %s: %s", channel_id, e)
