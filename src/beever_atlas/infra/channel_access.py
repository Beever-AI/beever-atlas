"""Channel-level access control guard (RES-177 H1).

``assert_channel_access`` is the single chokepoint every channel-scoped
public route funnels through before touching the stores. It raises
``HTTPException(403)`` unless:

1. The caller's principal owns (via ``PlatformConnection.owner_principal_id``)
   at least one connection whose ``selected_channels`` includes the target
   channel, OR
2. The deployment is in single-tenant compatibility mode
   (``BEEVER_SINGLE_TENANT=true``, default for v1.0) AND every matching
   connection row has its owner set to the shared sentinel
   ``"legacy:shared"`` (or ``None``) AND the caller is a user principal.

Bridge principals (``kind == "bridge"``) never own user channels; they
are rejected by the normal ownership check. In practice they are also
blocked by ``require_user`` once ``BEEVER_ALLOW_BRIDGE_AS_USER`` flips
to ``False`` (Group 6 follow-up).

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

    if not matching:
        logger.info(
            "channel_access deny: channel=%s principal=%s reason=no_matching_connection",
            channel_id,
            _principal_id(principal),
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Channel access denied",
        )

    pid = _principal_id(principal)
    kind = _principal_kind(principal)

    # Explicit ownership match wins for both user and bridge principals.
    for conn in matching:
        owner = getattr(conn, "owner_principal_id", None)
        if owner and owner == pid:
            return

    # Single-tenant fallback: user principals are admitted when every
    # matching row is un-owned or sentinel-owned.
    settings = get_settings()
    if getattr(settings, "beever_single_tenant", True) and kind == "user":
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
