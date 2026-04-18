"""Authentication dependencies for the FastAPI app.

API-key auth: `Authorization: Bearer <token>` validated against
`settings.api_keys` (env `BEEVER_API_KEYS`, comma-separated) using
`hmac.compare_digest` for constant-time comparison.

Two distinct dependencies are exposed so public user routes and internal
bridge routes use different trust models:

- `require_user(...)` → `Principal(kind="user")` for user-facing routes.
  REJECTS `BRIDGE_API_KEY` by default (security finding H4).
- `require_bridge(...)` → `Principal(kind="bridge")` for `/api/internal/*`.

`BEEVER_ALLOW_BRIDGE_AS_USER` is an emergency override that re-opens the
bridge-as-user path. It defaults to False; when set to True, `config.py`
emits a loud startup warning so operators notice the reopened gap.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Literal, Optional

from fastapi import Header, HTTPException, Query, status

from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)


class Principal(str):
    """Authenticated caller identity.

    Subclasses ``str`` so handler code that type-hints the result as
    ``str`` keeps working (logging, equality, formatting). The extra
    attributes ``.kind`` and ``.id`` expose the structured form when a
    caller explicitly needs it (e.g. `_assert_channel_access`).
    """

    __slots__ = ("_kind",)

    def __new__(
        cls, id: str, kind: Literal["user", "bridge", "mcp"]
    ) -> "Principal":
        inst = super().__new__(cls, id)
        inst._kind = kind
        return inst

    @property
    def kind(self) -> Literal["user", "bridge", "mcp"]:
        return self._kind  # type: ignore[return-value]

    @property
    def id(self) -> str:
        return str.__str__(self)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"Principal(kind={self._kind!r}, id={str.__str__(self)!r})"


_BRIDGE_PRINCIPAL_ID = "bridge"


def _parse_keys(raw: str) -> list[str]:
    return [k.strip() for k in raw.split(",") if k.strip()]


def _extract_bearer(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _principal_id_for_key(key: str) -> str:
    """Stable, non-reversible principal id derived from the raw API key.

    Using sha256 prevents key material from leaking into logs (the
    previous `key[:6]` prefix could collide across users).
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"user:{digest}"


def _principal_id_for_mcp_key(key: str) -> str:
    """Stable MCP principal id derived from the raw MCP bearer key.

    Mirrors :func:`_principal_id_for_key` but emits the ``mcp:`` prefix so
    :func:`~beever_atlas.infra.channel_access._principal_kind` resolves to
    ``"mcp"`` and the ACL guard applies MCP-strict rules (no single-tenant
    browsing fallback).
    """
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"mcp:{digest}"


def _match_user_key(token: str, keys: list[str]) -> Optional[Principal]:
    for key in keys:
        if hmac.compare_digest(token, key):
            return Principal(_principal_id_for_key(key), kind="user")
    return None


def _match_mcp_key(token: str, keys: list[str]) -> Optional[Principal]:
    """Return a ``Principal(kind='mcp')`` for a matching MCP bearer key.

    Mirrors :func:`_match_user_key`. Called by the MCP ASGI middleware, not
    by any FastAPI ``Depends`` — MCP principals never reach user-facing
    routes and vice versa.
    """
    for key in keys:
        if hmac.compare_digest(token, key):
            return Principal(_principal_id_for_mcp_key(key), kind="mcp")
    return None


def _match_bridge_key(token: str, bridge_key: str) -> Optional[Principal]:
    if bridge_key and hmac.compare_digest(token, bridge_key):
        return Principal(_BRIDGE_PRINCIPAL_ID, kind="bridge")
    return None


def _resolve_token(
    authorization: Optional[str],
    access_token: Optional[str],
    *,
    allow_query_string: bool,
) -> Optional[str]:
    token = _extract_bearer(authorization)
    if token:
        return token
    if allow_query_string and access_token:
        return access_token.strip() or None
    return None


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def require_user(
    authorization: Optional[str] = Header(default=None),
    access_token: Optional[str] = Query(
        default=None,
        description="Fallback auth for URLs consumed by browser loaders "
        "(<img src>, <a href>) that cannot carry custom headers.",
    ),
) -> Principal:
    """Validate Bearer token against configured user API keys.

    Accepts values from ``BEEVER_API_KEYS``. The internal
    ``BRIDGE_API_KEY`` is rejected here unless the emergency override
    ``BEEVER_ALLOW_BRIDGE_AS_USER=true`` is set (security finding H4) —
    that flag also triggers a boot-time warning in `config.py`.

    May be provided via ``Authorization: Bearer <token>`` (preferred) OR
    ``?access_token=<token>`` for `<img src>` / `<a href>` URLs that
    cannot set headers. Query-string hits are logged at INFO so operators
    can audit.
    """
    settings = get_settings()
    keys = _parse_keys(settings.api_keys)
    bridge_key = (settings.bridge_api_key or "").strip()
    if not keys and not bridge_key:
        raise _unauthorized("API authentication not configured")

    token = _resolve_token(authorization, access_token, allow_query_string=True)
    if not token:
        raise _unauthorized("Missing or malformed Authorization header")

    principal = _match_user_key(token, keys)
    if principal is not None:
        if not authorization and access_token:
            logger.info(
                "auth.query_string_user access principal=%s",
                principal.id,
            )
        return principal

    if settings.allow_bridge_as_user:
        bridge_principal = _match_bridge_key(token, bridge_key)
        if bridge_principal is not None:
            return bridge_principal

    raise _unauthorized("Invalid API key")


def require_user_optional(
    authorization: Optional[str] = Header(default=None),
    access_token: Optional[str] = Query(default=None),
) -> Optional[Principal]:
    """Like `require_user` but returns None instead of 401.

    Used by `/api/ask/shared/{token}` where auth is conditional on the
    share's visibility tier. Subject to the same
    ``BEEVER_ALLOW_BRIDGE_AS_USER`` gate as ``require_user``.
    """
    settings = get_settings()
    keys = _parse_keys(settings.api_keys)
    bridge_key = (settings.bridge_api_key or "").strip()

    token = _resolve_token(authorization, access_token, allow_query_string=True)
    if not token:
        return None

    principal = _match_user_key(token, keys)
    if principal is not None:
        return principal

    if settings.allow_bridge_as_user:
        return _match_bridge_key(token, bridge_key)
    return None


def require_bridge(
    authorization: Optional[str] = Header(default=None),
) -> Principal:
    """Validate Bearer token against the internal bridge key only.

    Mounted on ``/api/internal/*``. User API keys are rejected here so
    a leaked user token cannot reach internal routes, and the
    ``?access_token=`` query-string path is deliberately not accepted —
    internal callers always use the header.
    """
    settings = get_settings()
    bridge_key = (settings.bridge_api_key or "").strip()
    if not bridge_key:
        raise _unauthorized("Bridge authentication not configured")

    token = _extract_bearer(authorization)
    if not token:
        raise _unauthorized("Missing or malformed Authorization header")

    principal = _match_bridge_key(token, bridge_key)
    if principal is None:
        raise _unauthorized("Invalid bridge key")
    return principal


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
