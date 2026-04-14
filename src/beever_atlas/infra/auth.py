"""Authentication dependencies for the FastAPI app.

API-key auth (v0.1.0): `Authorization: Bearer <token>` validated against
`settings.api_keys` (env `BEEVER_API_KEYS`, comma-separated). Uses
`hmac.compare_digest` for constant-time comparison.
"""

from __future__ import annotations

import hmac
import logging
from typing import Optional

from fastapi import Header, HTTPException, Query, status

from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)


def _parse_keys(raw: str) -> list[str]:
    return [k.strip() for k in raw.split(",") if k.strip()]


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def require_user(
    authorization: Optional[str] = Header(default=None),
    access_token: Optional[str] = Query(
        default=None,
        description="Fallback auth for URLs consumed by browser loaders "
        "(<img src>, <a href>) that cannot carry custom headers.",
    ),
) -> str:
    """Validate Bearer token against configured API keys.

    Accepts either a user-facing API key (from `BEEVER_API_KEYS`) or the
    internal bridge shared secret (`BRIDGE_API_KEY`) used by the Discord/Slack
    bot service. Endpoints with additional internal-only checks (e.g.
    /api/internal/*) re-verify `BRIDGE_API_KEY` explicitly, so accepting it
    here just unblocks the first-layer gate without weakening those routes.

    The token may be provided via `Authorization: Bearer <token>` header
    (preferred) OR via `?access_token=<token>` query string (only for URLs
    consumed by browser-native loaders like <img> / <a href> that cannot
    set headers). The query-string path is equally validated but emits an
    info log so operators can audit.

    Returns a user identifier string on success, raises HTTP 401 otherwise.
    """
    settings = get_settings()
    keys = _parse_keys(settings.api_keys)
    bridge_key = (settings.bridge_api_key or "").strip()
    if not keys and not bridge_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API authentication not configured",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = _extract_bearer(authorization)
    if not token and access_token:
        token = access_token.strip() or None
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or malformed Authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    for key in keys:
        if hmac.compare_digest(token, key):
            return f"user:{key[:6]}"

    if bridge_key and hmac.compare_digest(token, bridge_key):
        return "service:bridge"

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid API key",
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_admin(
    x_admin_token: Optional[str] = Header(default=None, alias="X-Admin-Token"),
) -> str:
    """Validate admin token for dev-only endpoints."""
    settings = get_settings()
    expected = (settings.admin_token or "").strip()
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Admin token not configured",
        )
    if not x_admin_token or not hmac.compare_digest(x_admin_token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin token",
        )
    return "admin"
