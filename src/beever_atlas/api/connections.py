"""Platform connection management API endpoints."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.config import get_settings
from beever_atlas.infra.rate_limit import limiter
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter()

# Internal routes (bot → backend). Mounted separately in `server/app.py` with
# `Depends(require_bridge)` so they are NOT subject to the public
# `require_user` gate that now rejects BRIDGE_API_KEY (finding H4).
internal_router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------


class CreateConnectionRequest(BaseModel):
    platform: str
    display_name: str = ""
    credentials: dict[str, str]


class UpdateCredentialsRequest(BaseModel):
    """Partial credential update. Only the provided keys are merged over the
    stored credentials (e.g. add ``app_token`` to switch Slack to Socket Mode);
    omitted keys are preserved. Empty-string values are ignored, never stored,
    so a blank field in the UI means "keep existing" rather than "clear"."""

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


def _credential_fingerprint(platform: str, credentials: dict[str, str]) -> str | None:
    """Return a stable per-workspace identifier for a credentials dict.

    Used by `create_connection` to block creating a second row for the same
    external workspace. Returns None when no fingerprint is available, in which
    case the caller should not treat it as a collision.
    """
    if platform == "teams":
        # Microsoft App ID identifies the Azure Bot registration. The app
        # password (client secret) rotates independently, so it can't anchor
        # workspace identity.
        value = credentials.get("app_id") or credentials.get("appId")
        return value.strip().lower() if isinstance(value, str) and value.strip() else None
    value = credentials.get("bot_token")
    return value if isinstance(value, str) and value else None


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
    platform: str,
    credentials: dict,
    connection_id: str | None = None,
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


async def _unregister_adapter(connection_id: str) -> bool:
    """Call DELETE /bridge/adapters/{connectionId} to remove the adapter.

    Best-effort: errors are logged, never re-raised. Returns True when the
    adapter was unregistered (or was already absent — 404), False when the
    bridge could not be reached or returned an error — so callers can surface a
    clearer warning when a failed unregister precedes a destructive cascade.
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
                return False
            return True
        except httpx.ConnectError:
            logger.warning(
                "Bridge unreachable during adapter rollback for connection %s", connection_id
            )
            return False


async def _list_bridge_channels(
    platform: str,
    connection_id: str | None = None,
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
    # Mirrored from the Mongo document so the bot can seed its in-memory
    # `teamsKnownTeamIds` Map on startup, removing the dependency on
    # webhook reseeding or the @chat-adapter Redis cache (which is wiped
    # on Redis restart). Always present in the response for shape
    # stability; non-Teams platforms get an empty list.
    teams_known_team_ids: list[str] = []


@internal_router.get("/api/internal/connections/credentials")
async def list_connections_with_credentials() -> list[_InternalConnectionItem]:
    """Internal endpoint for bot startup sync — returns decrypted credentials.

    Secured by the router-level `require_bridge` dependency (mounted in
    `server/app.py`). Never expose to end users.
    """
    stores = get_stores()
    connections = await stores.platform.list_connections()
    result: list[_InternalConnectionItem] = []
    for conn in connections:
        if conn.status != "connected":
            continue
        try:
            creds = stores.platform.decrypt_connection_credentials(conn)
            result.append(
                _InternalConnectionItem(
                    connection_id=conn.id,
                    platform=conn.platform,
                    credentials=creds,
                    status=conn.status,
                    teams_known_team_ids=list(getattr(conn, "teams_known_team_ids", None) or []),
                )
            )
        except Exception as e:
            logger.warning("Failed to decrypt credentials for connection %s: %s", conn.id, e)
    return result


class _RecordTeamsKnownTeamRequest(BaseModel):
    """Body for the bot's write-through of an observed AAD group id."""

    aad_group_id: str


# A Microsoft Graph team-id is the team's AAD group object id — a GUID.
# Validate the shape here AND in the bot before persistence so a poisoned
# webhook (or a future Bot Framework change) can't inject an arbitrary
# value into Mongo or downstream Graph API paths.
_TEAMS_AAD_GROUP_ID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@internal_router.post(
    "/api/internal/connections/{connection_id}/teams-known-team-ids",
    status_code=204,
)
async def record_teams_known_team_id(
    connection_id: str,
    body: _RecordTeamsKnownTeamRequest,
) -> None:
    """Bot write-through: persist an AAD group id observed from a webhook.

    Called fire-and-forget from ``bridge.ts:recordTeamsConversation`` so
    that the team-id survives a Redis cache wipe AND a bot restart.
    Idempotent via ``$addToSet`` in the store. Returns 404 when the
    connection id is unknown, 400 when the AAD group id doesn't match
    the expected shape, and 422 when the connection isn't a Teams row.

    Secured by the router-level ``require_bridge`` dependency. Never
    expose to end users.
    """
    aad_group_id = body.aad_group_id.strip()
    if not _TEAMS_AAD_GROUP_ID_RE.match(aad_group_id):
        raise HTTPException(
            status_code=400,
            detail="aad_group_id must be a Microsoft AAD group GUID",
        )

    stores = get_stores()
    existing = await stores.platform.get_connection(connection_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="connection not found")
    if existing.platform != "teams":
        raise HTTPException(
            status_code=422,
            detail=f"connection {connection_id} is not a Teams connection",
        )

    await stores.platform.add_teams_known_team_id(connection_id, aad_group_id)


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

    # Duplicate check: reject if another connection already identifies the same
    # external workspace. Platforms differ in which credential field anchors
    # workspace identity:
    #   - Slack/Discord/Mattermost/Telegram: bot token
    #   - Teams: Microsoft App ID (the secret rotates independently)
    # Without this per-platform fingerprint, resubmitting the Teams wizard with
    # the same app produced two DB rows both pointing at the same Azure Bot,
    # and only one of them ever received webhooks.
    new_fingerprint = _credential_fingerprint(platform, body.credentials)
    if new_fingerprint:
        existing = await stores.platform.list_connections()
        for existing_conn in existing:
            if existing_conn.status != "connected" or existing_conn.platform != platform:
                continue
            try:
                existing_creds = stores.platform.decrypt_connection_credentials(existing_conn)
                existing_fingerprint = _credential_fingerprint(platform, existing_creds)
                if existing_fingerprint and existing_fingerprint == new_fingerprint:
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
    await _refresh_proxy_hosts(stores)
    return _to_response(conn)


async def _refresh_proxy_hosts(stores) -> None:
    """Re-derive the file/media-proxy host allowlist after a connection
    write. Failures are logged and swallowed so a transient store error
    cannot block the user-visible CRUD response."""
    try:
        from beever_atlas.infra.platform_hosts import refresh_runtime_proxy_hosts

        await refresh_runtime_proxy_hosts(stores)
    except Exception:
        logger.exception("connections: failed to refresh proxy host allowlist (non-fatal)")


@router.patch("/api/connections/{connection_id}/credentials", response_model=ConnectionResponse)
async def update_connection_credentials(
    connection_id: str,
    body: UpdateCredentialsRequest,
    principal: Principal = Depends(require_user),
) -> ConnectionResponse:
    """Rotate/extend a connection's credentials IN PLACE — no delete, no cascade.

    The only non-destructive way to change credentials: deleting a connection
    cascade-purges its sole-owned channels' data. This merges the provided
    non-empty keys over the stored credentials (e.g. add ``app_token`` to flip
    Slack to Socket Mode), re-registers the adapter so the running bot rebuilds
    with the change, and persists — leaving channels and synced data intact.
    """
    from beever_atlas.capabilities.errors import ConnectionAccessDenied
    from beever_atlas.infra.channel_access import assert_connection_owned

    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    # Owner-gated, mirroring delete_connection: only the owner may rotate creds.
    # (Single-tenant fallback admits legacy/un-owned rows.)
    try:
        await assert_connection_owned(principal, connection_id)
    except ConnectionAccessDenied as e:
        raise HTTPException(
            status_code=403, detail="You do not have access to this connection."
        ) from e

    # Merge only non-empty provided keys; a blank field means "keep existing".
    incoming = {k: v for k, v in body.credentials.items() if isinstance(v, str) and v.strip()}
    if not incoming:
        raise HTTPException(status_code=422, detail="No credential values provided to update.")

    try:
        merged = stores.platform.decrypt_connection_credentials(conn)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail="Could not read existing credentials for this connection."
        ) from e
    merged.update(incoming)

    # Re-register so the running bot rebuilds the adapter with the new creds
    # (raises HTTPException on bridge failure — surfaced to the UI).
    await _register_adapter(conn.platform, merged, connection_id=connection_id)

    updated = await stores.platform.update_connection(
        connection_id, credentials=merged, status="connected"
    )
    if updated is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    logger.info(
        "Updated credentials for connection id=%s platform=%s (keys=%s)",
        connection_id,
        conn.platform,
        sorted(incoming.keys()),
    )
    await _refresh_proxy_hosts(stores)
    return _to_response(updated)


@router.delete("/api/connections/{connection_id}", status_code=204)
async def delete_connection(
    connection_id: str,
    cascade: bool = Query(
        default=True,
        description=(
            "When true (default), hard-purge channels SOLELY owned by this "
            "connection (no other connection lists them in selected_channels). "
            "Shared channels are never purged. Set false to skip the cascade."
        ),
    ),
    principal: Principal = Depends(require_user),
) -> None:
    """Disconnect and remove a platform connection.

    delete-channel-v2 Wave 3: when ``cascade`` is true (default), channels
    SOLELY owned by this connection are hard-purged through
    :func:`beever_atlas.services.channel_deletion.purge_channel`. "Sole-owned"
    = no OTHER remaining connection's ``selected_channels`` lists the channel.
    Shared channels (still referenced elsewhere) are left untouched — we don't
    even call purge for them. Each purge is best-effort: a failure is logged
    and never blocks the connection delete (still returns 204).
    """
    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    # Destructive authz FIRST: only the connection's owner may delete it (and
    # cascade-purge its sole-owned channels). Without this, any authenticated
    # user could delete another tenant's connection by id — the per-channel
    # guard in the cascade below runs only AFTER the connection row is already
    # gone, so it cannot protect the connection itself. Mirrors the ownership
    # check ``GET /api/connections/{id}/channels`` already enforces; safe in
    # single-tenant mode (the helper admits un-owned/legacy connections).
    # NOTE: ``assert_connection_owned`` raises the capability-layer
    # ``ConnectionAccessDenied`` (no global REST handler translates it, unlike
    # ``assert_channel_delete_access`` which raises ``HTTPException`` directly),
    # so translate it to a 403 here.
    from beever_atlas.capabilities.errors import ConnectionAccessDenied
    from beever_atlas.infra.channel_access import assert_connection_owned

    try:
        await assert_connection_owned(principal, connection_id)
    except ConnectionAccessDenied:
        raise HTTPException(
            status_code=403,
            detail="You do not have permission to delete this connection.",
        ) from None

    # Compute channels solely owned by this connection BEFORE deletion, so the
    # "is it shared?" scan sees the other connections as they stand now. A
    # channel is sole-owned iff no OTHER connection lists it in selected_channels.
    sole_owned: list[str] = []
    if cascade and getattr(conn, "selected_channels", None):
        all_connections = await stores.platform.list_connections()
        others = [c for c in all_connections if c.id != connection_id]
        for channel_id in conn.selected_channels:
            shared = any(
                channel_id in (getattr(other, "selected_channels", None) or []) for other in others
            )
            if not shared:
                sole_owned.append(channel_id)

    # Unregister from bot service (best-effort — don't fail if bot is down).
    # If unregister fails AND we're about to cascade-purge sole-owned channels,
    # warn loudly: the bot may keep routing messages to the now-purged channels
    # until its adapter registry is cleared (bot restart / next credential sync).
    unregistered = await _unregister_adapter(conn.id)
    if not unregistered and sole_owned:
        logger.warning(
            "connection delete: bridge adapter unregister FAILED for connection %s "
            "while cascading a hard-purge of %d sole-owned channel(s) %s — the bot "
            "may keep routing to these purged channels until its adapter is cleared "
            "(restart the bot or wait for the next credential sync).",
            connection_id,
            len(sole_owned),
            sole_owned,
        )

    deleted = await stores.platform.delete_connection(connection_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    logger.info("Deleted platform connection id=%s platform=%s", connection_id, conn.platform)
    await _refresh_proxy_hosts(stores)

    # Cascade: hard-purge each sole-owned channel best-effort. A purge failure
    # must NOT fail the connection delete (the connection is already gone).
    if sole_owned:
        from beever_atlas.infra.channel_access import assert_channel_delete_access
        from beever_atlas.services.channel_deletion import purge_channel

        principal_id = getattr(principal, "id", None) or str(principal) or "connection_delete"
        for channel_id in sole_owned:
            # Per-channel destructive authz: a purge is irreversible, so re-check
            # the SAME guard the direct DELETE /api/channels/{id} path enforces
            # for each cascaded channel. On denial we SKIP that one channel (log
            # + continue) rather than 403'ing the whole connection delete — the
            # connection is already gone and other sole-owned channels the
            # principal CAN delete should still be purged.
            try:
                await assert_channel_delete_access(principal, channel_id)
            except HTTPException:
                logger.warning(
                    "connection delete cascade: skipping channel %s — principal "
                    "%s lacks destructive authz (connection %s)",
                    channel_id,
                    principal_id,
                    connection_id,
                )
                continue
            try:
                await purge_channel(channel_id, principal_id=principal_id)
            except Exception:
                logger.exception(
                    "connection delete cascade: purge failed for sole-owned "
                    "channel %s (connection %s) — continuing",
                    channel_id,
                    connection_id,
                )


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
    await _refresh_proxy_hosts(stores)
    return _to_response(updated)


@router.get("/api/connections/{connection_id}/channels", response_model=list[ChannelItem])
@limiter.limit("20/minute")
async def list_connection_channels(
    request: Request,  # noqa: ARG001 — required by slowapi to identify the caller
    connection_id: str,
) -> list[ChannelItem]:
    """List available channels for a platform connection.

    Rate-limited to 20/minute per client (RES-286 security review): the FE
    Refresh button can be clicked rapidly, and each call proxies to the
    upstream Mattermost/Slack/etc. API. The cap prevents a runaway client
    from exhausting the bot token's upstream rate limit.
    """
    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)
    if conn is None:
        raise HTTPException(status_code=404, detail=f"Connection {connection_id!r} not found")

    # Teams and Telegram both fall through: the bridge returns an in-memory
    # registry of conversations the bot has received activities from. For Teams,
    # conversations also get back-filled by Microsoft Graph via the Chat SDK
    # adapter when credentials + consent are in place.
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


async def _trigger_sync_for_channels(
    channel_ids: list[str], connection_id: str | None = None
) -> None:
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
