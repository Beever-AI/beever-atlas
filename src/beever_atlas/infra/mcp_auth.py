"""ASGI middleware for authenticating requests to the ``/mcp`` mount.

Why a dedicated middleware:
  FastAPI ``Depends(...)`` applied via ``include_router(..., dependencies=...)``
  does NOT propagate to ``app.mount(...)`` sub-applications — Starlette Mounts
  are a separate ASGI tree. A prior ``/mcp`` mount relied on that false
  assumption and was served unauthenticated; it has since been retired. This
  middleware is the structural fix: it runs at the ASGI layer, BEFORE FastMCP
  dispatches any protocol message, so unauthenticated requests never reach
  tool handling.

Acceptance criteria (from ``specs/mcp-auth/spec.md``):
  - Missing/invalid Bearer → 401 with ``WWW-Authenticate: Bearer realm="atlas-mcp"``
  - Query-string credentials (``?access_token=``, ``?api_key=``) are rejected
    regardless of Authorization header state
  - Valid token → principal ``mcp:<sha256(token)[:16]>`` injected into the ASGI
    scope; raw ``Authorization`` header stripped from the scope so downstream
    handlers cannot read it
  - Constant-time token comparison via ``hmac.compare_digest``
  - Brute-force watchdog: after 5 failures from the same IP in a 60s window,
    emit ONE ``WARNING`` per minute; does not block requests
  - MCP keys are sourced ONLY from ``BEEVER_MCP_API_KEYS``; user keys and the
    bridge key are rejected here (confirmed by ``config.validate_keys_disjoint``)
"""

from __future__ import annotations

import logging
import time
import uuid

from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from beever_atlas.infra.auth import _match_mcp_key, _parse_keys
from beever_atlas.infra.config import get_settings

logger = logging.getLogger(__name__)


# --- Brute-force watchdog ---------------------------------------------------

_BRUTEFORCE_WINDOW_SECONDS = 60
_BRUTEFORCE_THRESHOLD = 5
_BRUTEFORCE_ALERT_COOLDOWN_SECONDS = 60

# Map of client IP -> list of failure timestamps in the current window.
_ip_failures: dict[str, list[float]] = {}
# Map of client IP -> timestamp of the last emitted bruteforce WARNING.
_ip_last_alert: dict[str, float] = {}


def _prune_ip_failures(ip: str, now: float) -> list[float]:
    """Return the failure list for ``ip`` with stale entries pruned."""
    window_start = now - _BRUTEFORCE_WINDOW_SECONDS
    entries = [t for t in _ip_failures.get(ip, ()) if t >= window_start]
    _ip_failures[ip] = entries
    return entries


def _record_auth_failure(ip: str) -> None:
    """Record a failure for ``ip`` and emit a bruteforce WARNING if the
    5-in-60s threshold is hit, rate-limited to one alert per minute per IP."""
    now = time.monotonic()
    entries = _prune_ip_failures(ip, now)
    entries.append(now)
    if len(entries) >= _BRUTEFORCE_THRESHOLD:
        last_alert = _ip_last_alert.get(ip, 0.0)
        if now - last_alert >= _BRUTEFORCE_ALERT_COOLDOWN_SECONDS:
            logger.warning(
                "event=mcp_auth_bruteforce_suspected ip=%s window_seconds=%d failure_count=%d",
                ip,
                _BRUTEFORCE_WINDOW_SECONDS,
                len(entries),
            )
            _ip_last_alert[ip] = now


def _reset_watchdog_for_tests() -> None:
    """Clear the in-memory bruteforce state; for unit tests only."""
    _ip_failures.clear()
    _ip_last_alert.clear()


# --- Token extraction -------------------------------------------------------


def _extract_bearer(authorization: str | None) -> str | None:
    if not authorization:
        return None
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _client_ip(scope: Scope) -> str:
    """Best-effort client IP derivation from the ASGI scope."""
    client = scope.get("client")
    if client and isinstance(client, tuple) and client[0]:
        return str(client[0])
    return "unknown"


def _unauthorized(
    request_id: str,
    reason: str,
    *,
    detail: str = "Missing or invalid MCP bearer",
) -> JSONResponse:
    """Build the 401 response. The WWW-Authenticate header signals that this
    endpoint expects Bearer credentials — MCP clients key off this to decide
    whether to retry with a configured token."""
    return JSONResponse(
        status_code=401,
        content={
            "error": "mcp_unauthorized",
            "detail": detail,
            "reason": reason,
            "request_id": request_id,
        },
        headers={"WWW-Authenticate": 'Bearer realm="atlas-mcp"'},
    )


# --- Middleware -------------------------------------------------------------


class MCPAuthMiddleware:
    """ASGI middleware that authenticates every request to the MCP mount.

    Instantiated once by Starlette when mounted via
    ``http_app(middleware=[Middleware(MCPAuthMiddleware)])``. Each request
    invokes ``__call__``; successful requests are forwarded with the
    authorization header scrubbed and the principal id attached to
    ``scope["state"]``.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only enforce on HTTP — websocket / lifespan pass through.
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope=scope, receive=receive)
        request_id = str(uuid.uuid4())
        ip = _client_ip(scope)

        # Reject query-string credentials outright, regardless of header.
        for qname in ("access_token", "api_key"):
            if qname in request.query_params:
                logger.warning(
                    "event=mcp_auth_failure principal=anonymous "
                    "reason=query_string_credential request_id=%s ip=%s",
                    request_id,
                    ip,
                )
                _record_auth_failure(ip)
                await _unauthorized(
                    request_id,
                    "query_string_credentials_not_allowed",
                    detail="Credentials must be sent via the Authorization header",
                )(scope, receive, send)
                return

        # Extract Bearer token.
        authorization = request.headers.get("authorization")
        token = _extract_bearer(authorization)
        if not token:
            logger.warning(
                "event=mcp_auth_failure principal=anonymous "
                "reason=missing_bearer request_id=%s ip=%s",
                request_id,
                ip,
            )
            _record_auth_failure(ip)
            await _unauthorized(request_id, "missing_bearer")(scope, receive, send)
            return

        settings = get_settings()
        keys = _parse_keys(settings.beever_mcp_api_keys)
        if not keys:
            logger.error(
                "event=mcp_auth_failure principal=anonymous "
                "reason=no_mcp_keys_configured request_id=%s ip=%s",
                request_id,
                ip,
            )
            await _unauthorized(
                request_id,
                "no_mcp_keys_configured",
                detail="MCP authentication not configured on this server",
            )(scope, receive, send)
            return

        principal = _match_mcp_key(token, keys)
        if principal is None:
            logger.warning(
                "event=mcp_auth_failure principal=anonymous "
                "reason=invalid_bearer request_id=%s ip=%s",
                request_id,
                ip,
            )
            _record_auth_failure(ip)
            await _unauthorized(request_id, "invalid_bearer")(scope, receive, send)
            return

        # Strip the Authorization header so tool handlers cannot observe
        # the raw token (defense in depth against accidental logging).
        scrubbed_headers = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() != b"authorization"
        ]
        scrubbed_scope = {**scope, "headers": scrubbed_headers}

        # Attach principal id to ASGI scope.state for tool handlers.
        state = dict(scrubbed_scope.get("state") or {})
        state["mcp_principal_id"] = principal.id
        state["mcp_principal_kind"] = principal.kind
        state["mcp_request_id"] = request_id
        scrubbed_scope["state"] = state

        logger.debug(
            "event=mcp_auth_success principal=%s request_id=%s ip=%s",
            principal.id,
            request_id,
            ip,
        )

        await self.app(scrubbed_scope, receive, send)


__all__ = [
    "MCPAuthMiddleware",
    "_reset_watchdog_for_tests",
]
