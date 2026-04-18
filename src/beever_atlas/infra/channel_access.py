"""Channel-level access control guard (RES-177 H1).

``assert_channel_access`` is the single chokepoint every channel-scoped
public route funnels through before touching the stores.

`PlatformConnection.selected_channels` is the **sync pick-list**, not the
authoritative set of channels a connection can reach: a Slack/Discord
connection legitimately BROWSES every channel its bot sees. That means
"channel has no matching connection in selected_channels" is not, on its
own, a cross-tenant exploit signal — it just means the caller is
browsing a channel they haven't synced yet. The guard therefore decides
as follows.

Allow when any of the following holds:

1. The caller's principal owns (via ``PlatformConnection.owner_principal_id``)
   at least one connection whose ``selected_channels`` includes the target
   channel.
2. The deployment is in single-tenant compatibility mode
   (``BEEVER_SINGLE_TENANT=true``, default for v1.0), the caller is a
   user principal, AND EITHER
   - every matching connection has ``owner_principal_id`` in
     ``{None, "legacy:shared"}``, OR
   - no connection currently lists the channel in ``selected_channels``
     (browsing path — the single operator is discovering a new channel
     before adding it to a sync pick-list).

Reject when:

- The caller is a bridge principal and there is no explicit owner match
  (bridge principals never inherit user channel ownership).
- The deployment is in multi-tenant mode (``BEEVER_SINGLE_TENANT=false``)
  and no matching connection is owned by the caller.

In practice bridge principals are also blocked upstream by ``require_user``
once ``BEEVER_ALLOW_BRIDGE_AS_USER`` flips to ``False`` (Group 6 did this).

The guard is async because it reads from MongoDB via the platform store
singleton. No caching layer is added here — the platform store already
relies on Mongo's own indexes and connections are small (handful per
deployment).
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, status

from beever_atlas.infra.auth import Principal
from beever_atlas.infra.config import get_settings
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)


_LEGACY_SHARED_OWNER = "legacy:shared"


def _principal_kind(principal: Principal | str) -> str:
    """Return the principal kind ('user' / 'bridge' / 'unknown').

    Accepts bare strings (test conftests that pre-date the Principal type)
    and treats them as ``'user'`` to preserve backward compatibility.
    """
    kind = getattr(principal, "kind", None)
    if kind in ("user", "bridge"):
        return kind  # type: ignore[return-value]
    return "user"


def _principal_id(principal: Principal | str) -> str:
    """Return the principal id; falls back to ``str(principal)``."""
    pid = getattr(principal, "id", None)
    if pid:
        return str(pid)
    return str(principal)


async def assert_channel_access(
    principal: Principal | str, channel_id: str
) -> None:
    """Raise ``HTTPException(403)`` if ``principal`` cannot access ``channel_id``.

    See module docstring for the full decision table.
    """
    stores = get_stores()
    connections = await stores.platform.list_connections()
    matching = [c for c in connections if channel_id in (c.selected_channels or [])]

    pid = _principal_id(principal)
    kind = _principal_kind(principal)
    settings = get_settings()
    single_tenant = bool(getattr(settings, "beever_single_tenant", True))

    if not matching:
        # No connection explicitly claims this channel. `selected_channels`
        # is a sync pick-list, not the authoritative access list — a
        # single-tenant operator legitimately browses channels before
        # syncing them. Allow user principals through in single-tenant
        # mode; in multi-tenant mode the operator must have explicitly
        # claimed the channel via `selected_channels` on an owned
        # connection first.
        if single_tenant and kind == "user":
            return
        logger.info(
            "channel_access deny: channel=%s principal=%s kind=%s reason=no_matching_connection",
            channel_id,
            pid,
            kind,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Channel access denied",
        )

    # Explicit ownership match wins for both user and bridge principals.
    for conn in matching:
        owner = getattr(conn, "owner_principal_id", None)
        if owner and owner == pid:
            return

    # Single-tenant fallback: user principals are admitted when every
    # matching row is un-owned or sentinel-owned.
    if single_tenant and kind == "user":
        all_legacy = all(
            (getattr(c, "owner_principal_id", None) in (None, _LEGACY_SHARED_OWNER))
            for c in matching
        )
        if all_legacy:
            return

    logger.info(
        "channel_access deny: channel=%s principal=%s kind=%s reason=owner_mismatch",
        channel_id,
        pid,
        kind,
    )
    raise HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail="Channel access denied",
    )


async def assert_connection_owned(
    principal: Principal | str, connection_id: str
) -> None:
    """Raise ``ConnectionAccessDenied`` if ``principal`` doesn't own ``connection_id``.

    Mirrors :func:`assert_channel_access` semantics for connection-scoped
    operations (e.g. listing the channels of a single connection):

    Allow when:

    1. The connection's ``owner_principal_id`` equals ``principal.id``.
    2. Single-tenant mode (``BEEVER_SINGLE_TENANT=true``), the principal is
       a ``user`` kind, AND the connection is un-owned
       (``owner_principal_id in {None, "legacy:shared"}``) — the legacy
       fallback for pre-migration rows and env-provisioned connections.

    Bridge principals never inherit the single-tenant fallback.

    Raises:
        ConnectionAccessDenied: when the connection does not exist OR the
            principal is not permitted to access it. The two cases are not
            distinguished in the raised exception so callers cannot probe
            for connection existence without ownership.
    """
    # Import here to avoid a circular import: `capabilities.errors` is a
    # small leaf module but keeping this local makes the dependency
    # direction obvious (infra → capabilities.errors, not the reverse).
    from beever_atlas.capabilities.errors import ConnectionAccessDenied

    stores = get_stores()
    conn = await stores.platform.get_connection(connection_id)

    pid = _principal_id(principal)
    kind = _principal_kind(principal)
    settings = get_settings()
    single_tenant = bool(getattr(settings, "beever_single_tenant", True))

    if conn is None:
        logger.info(
            "connection_access deny: connection=%s principal=%s kind=%s reason=not_found",
            connection_id,
            pid,
            kind,
        )
        raise ConnectionAccessDenied(connection_id)

    owner = getattr(conn, "owner_principal_id", None)

    # Explicit ownership match wins for any principal kind.
    if owner and owner == pid:
        return

    # Single-tenant fallback: user principals are admitted on un-owned /
    # sentinel-owned rows. Bridge (and future MCP) principals don't inherit
    # this — they must have an explicit match.
    if (
        single_tenant
        and kind == "user"
        and owner in (None, _LEGACY_SHARED_OWNER)
    ):
        return

    logger.info(
        "connection_access deny: connection=%s principal=%s kind=%s reason=owner_mismatch",
        connection_id,
        pid,
        kind,
    )
    raise ConnectionAccessDenied(connection_id)
