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

from fastapi import Header, HTTPException, Query, Request, status

from beever_atlas.infra.config import get_settings
from beever_atlas.infra.loader_token import verify_loader_token

logger = logging.getLogger(__name__)


class Principal(str):
    """Authenticated caller identity.

    Subclasses ``str`` so handler code that type-hints the result as
    ``str`` keeps working (logging, equality, formatting). The extra
    attributes ``.kind`` and ``.id`` expose the structured form when a
    caller explicitly needs it (e.g. `_assert_channel_access`).
    """

    __slots__ = ("_kind",)

    def __new__(cls, id: str, kind: Literal["user", "bridge", "mcp"]) -> "Principal":
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
    previous ``key[:6]`` prefix could collide across users).

    Why SHA-256 here is correct (not a CodeQL ``py/weak-sensitive-data-
    hashing`` issue, alerts #42 / #43):

      * ``key`` is a pre-shared bearer token loaded from
        ``BEEVER_API_KEYS``. By the time this function runs the token has
        ALREADY been verified against the configured keyset via
        ``hmac.compare_digest`` in :func:`_match_user_key`. We are not
        hashing a user-chosen password to store-and-later-verify — that
        is the use case CodeQL's rule targets.
      * The output is used solely as a stable, non-reversible
        identifier. Properties needed: deterministic per key, low
        collision probability across the configured keyset (≤ a few
        hundred entries), and resistance to log-leak inversion. Plain
        SHA-256 satisfies all three.
      * The output is persisted as ``PlatformConnection.owner_principal_id``
        for channel-access ownership checks (see
        :mod:`beever_atlas.infra.channel_access`). Switching to a slow
        KDF (Argon2 / bcrypt / PBKDF2) or to HMAC with a server-side
        secret would change the output bytes, invalidating every
        existing ownership record and locking users out of their
        connections — a functional regression we explicitly avoid.
      * Slow KDFs and HMAC-with-secret defend against offline
        brute-force of leaked password databases. Neither threat applies
        here: there is no database of these digests, and the tokens
        they're derived from are server-config bearer tokens, not user
        passwords subject to dictionary attack.

    If a future change requires a stronger primitive, it must come with
    a migration plan for ``owner_principal_id`` (e.g. dual-read v1/v2
    during a deprecation window).
    """
    # lgtm[py/weak-sensitive-data-hashing] -- intentional: see docstring above.
    # SHA-256 here derives a stable, non-reversible identifier from an
    # already-verified bearer token; it is NOT password storage. Changing
    # the algorithm would invalidate every persisted owner_principal_id.
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"user:{digest}"


def _principal_id_for_mcp_key(key: str) -> str:
    """Stable MCP principal id derived from the raw MCP bearer key.

    Mirrors :func:`_principal_id_for_key` but emits the ``mcp:`` prefix so
    :func:`~beever_atlas.infra.channel_access._principal_kind` resolves to
    ``"mcp"``. In single-tenant mode the MCP principal inherits the
    legacy/un-owned connection fallback alongside ``user`` principals so
    list/access calls agree on what's visible. In multi-tenant mode the
    MCP principal must own each connection explicitly. Bridge principals
    are always strict.

    The SHA-256 derivation rationale and migration constraint are
    documented on :func:`_principal_id_for_key` — same primitive, same
    reasoning. CodeQL alert #43 is a false positive for the same reason
    as #42.
    """
    # lgtm[py/weak-sensitive-data-hashing] -- intentional: see _principal_id_for_key
    # docstring. Mirror function; same rationale, same migration constraint.
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
        description="DEPRECATED on this dependency. Header-only — use "
        "`require_user_loader` for endpoints consumed by browser loaders "
        "(<img src>, <a href>) that cannot carry custom headers.",
        include_in_schema=False,
    ),
) -> Principal:
    """Validate Bearer token against configured user API keys.

    Accepts values from ``BEEVER_API_KEYS``. The internal
    ``BRIDGE_API_KEY`` is rejected here unless the emergency override
    ``BEEVER_ALLOW_BRIDGE_AS_USER=true`` is set (security finding H4) —
    that flag also triggers a boot-time warning in `config.py`.

    Header-only: ``Authorization: Bearer <token>``. The legacy
    ``?access_token=<token>`` query-string path is REJECTED here (issue
    #88 — narrows the credential-leak surface). Endpoints consumed by
    browser-native loaders (``<img src>``, ``<a href>``) MUST use
    ``require_user_loader`` instead.
    """
    settings = get_settings()
    keys = _parse_keys(settings.api_keys)
    bridge_key = (settings.bridge_api_key or "").strip()
    if not keys and not bridge_key:
        raise _unauthorized("API authentication not configured")

    # Surface misconfigured callers: if a request is RELYING on the
    # query-string token (no header sent), log so operators can spot it.
    # Dual-auth callers (header + query string) don't trip this — they're
    # already presenting the header path.
    if access_token and not authorization:
        logger.info(
            "auth.query_string_rejected path=require_user — "
            "caller sent ?access_token= on a header-only endpoint"
        )

    token = _resolve_token(authorization, access_token, allow_query_string=False)
    if not token:
        raise _unauthorized("Missing or malformed Authorization header")

    principal = _match_user_key(token, keys)
    if principal is not None:
        return principal

    if settings.allow_bridge_as_user:
        bridge_principal = _match_bridge_key(token, bridge_key)
        if bridge_principal is not None:
            return bridge_principal

    raise _unauthorized("Invalid API key")


def require_user_optional(
    authorization: Optional[str] = Header(default=None),
    access_token: Optional[str] = Query(default=None, include_in_schema=False),
) -> Optional[Principal]:
    """Like `require_user` but returns None instead of 401.

    Header-only after issue #88. Endpoints whose auth is conditional AND
    that need to support browser-native loaders (e.g. shared-link visits)
    MUST use ``require_user_loader_optional`` instead.
    """
    settings = get_settings()
    keys = _parse_keys(settings.api_keys)
    bridge_key = (settings.bridge_api_key or "").strip()

    if access_token and not authorization:
        logger.info(
            "auth.query_string_rejected path=require_user_optional — "
            "caller sent ?access_token= on a header-only endpoint"
        )

    token = _resolve_token(authorization, access_token, allow_query_string=False)
    if not token:
        return None

    principal = _match_user_key(token, keys)
    if principal is not None:
        return principal

    if settings.allow_bridge_as_user:
        return _match_bridge_key(token, bridge_key)
    return None


def _try_signed_loader_token(
    loader_token: Optional[str],
    *,
    current_path: str,
    secret: str,
) -> Optional[Principal]:
    """Verify a signed `?loader_token=` and return a Principal on success.

    Returns ``None`` for missing token, missing secret, or any verification
    failure. The caller then decides whether to fall back to raw-key
    matching (governed by ``BEEVER_LOADER_RAW_KEY_FALLBACK``).

    Issue #89 — caller is responsible for the multi-secret rotation loop:
    when rotating ``LOADER_TOKEN_SECRET``, call this helper twice (new
    secret first, then old) before returning ``None``. Keeping the loop
    in the caller avoids a breaking-signature change on
    ``verify_loader_token``.
    """
    if not loader_token or not secret:
        return None
    user_id = verify_loader_token(loader_token, current_path=current_path, secret=secret)
    if user_id is None:
        return None
    logger.info("auth.loader_token_verified principal=%s", user_id)
    return Principal(user_id, kind="user")


def require_user_loader(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    access_token: Optional[str] = Query(
        default=None,
        description="Fallback auth for URLs consumed by browser loaders "
        "(<img src>, <a href>) that cannot carry custom headers. Accepted "
        "only while BEEVER_LOADER_RAW_KEY_FALLBACK=true (issue #89).",
    ),
    loader_token: Optional[str] = Query(
        default=None,
        description="HMAC-signed scoped token from POST /api/auth/loader-token. "
        "Preferred over `access_token` — short-lived, route-bound. Issue #89.",
    ),
) -> Principal:
    """Validate caller via signed loader token, header, or (legacy) query string.

    Verification order (issue #89):

      1. ``?loader_token=`` — HMAC-signed, route-bound, 5-min TTL. Preferred.
      2. ``Authorization: Bearer`` header — works on the loader router too.
      3. ``?access_token=`` — legacy raw user API key, accepted ONLY while
         ``BEEVER_LOADER_RAW_KEY_FALLBACK=true`` (the migration default).

    Use ONLY on endpoints consumed by browser-native loaders (``<img src>``,
    ``<a href>``) that cannot carry custom ``Authorization`` headers. All
    other user-facing routes should use ``require_user`` (header-only).
    """
    settings = get_settings()
    keys = _parse_keys(settings.api_keys)
    bridge_key = (settings.bridge_api_key or "").strip()
    if not keys and not bridge_key:
        raise _unauthorized("API authentication not configured")

    # 1. Signed loader token wins outright when present and valid.
    secret = (settings.loader_token_secret or "").strip()
    signed = _try_signed_loader_token(loader_token, current_path=request.url.path, secret=secret)
    if signed is not None:
        return signed

    # 2. Header auth (always allowed; same matching rules as `require_user`).
    header_token = _extract_bearer(authorization)
    if header_token:
        principal = _match_user_key(header_token, keys)
        if principal is not None:
            return principal
        if settings.allow_bridge_as_user:
            bridge_principal = _match_bridge_key(header_token, bridge_key)
            if bridge_principal is not None:
                return bridge_principal
        raise _unauthorized("Invalid API key")

    # 3. Legacy raw `?access_token=` — only when fallback flag is on.
    if access_token and settings.loader_raw_key_fallback:
        raw = (access_token or "").strip()
        if raw:
            principal = _match_user_key(raw, keys)
            if principal is not None:
                logger.info("auth.loader_fallback_raw_key principal=%s", principal.id)
                return principal
            if settings.allow_bridge_as_user:
                bridge_principal = _match_bridge_key(raw, bridge_key)
                if bridge_principal is not None:
                    logger.info(
                        "auth.loader_fallback_raw_key principal=%s",
                        bridge_principal.id,
                    )
                    return bridge_principal
        raise _unauthorized("Invalid API key")

    raise _unauthorized("Missing or malformed loader credential")


def require_user_loader_optional(
    request: Request,
    authorization: Optional[str] = Header(default=None),
    access_token: Optional[str] = Query(default=None),
    loader_token: Optional[str] = Query(default=None),
) -> Optional[Principal]:
    """Like ``require_user_loader`` but returns None instead of 401.

    Accepts the same three credential surfaces (signed token, header, legacy
    raw key when the fallback flag is on) and returns ``None`` instead of
    raising for missing or invalid credentials. Used by the public
    shared-conversation endpoint, where auth is conditional on the share
    visibility tier.
    """
    settings = get_settings()
    keys = _parse_keys(settings.api_keys)
    bridge_key = (settings.bridge_api_key or "").strip()

    secret = (settings.loader_token_secret or "").strip()
    signed = _try_signed_loader_token(loader_token, current_path=request.url.path, secret=secret)
    if signed is not None:
        return signed

    header_token = _extract_bearer(authorization)
    if header_token:
        principal = _match_user_key(header_token, keys)
        if principal is not None:
            return principal
        if settings.allow_bridge_as_user:
            return _match_bridge_key(header_token, bridge_key)
        return None

    if access_token and settings.loader_raw_key_fallback:
        raw = (access_token or "").strip()
        if raw:
            principal = _match_user_key(raw, keys)
            if principal is not None:
                return principal
            if settings.allow_bridge_as_user:
                return _match_bridge_key(raw, bridge_key)
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
