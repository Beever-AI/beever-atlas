"""Helpers for the authenticated media proxy.

The route handler `proxy_media` lives in `beever_atlas.api.loaders` (a
dedicated browser-loader router that uses `require_user_loader` instead
of the standard header-only `require_user`). This module owns the shared
state — the Slack-host allow-list, the multi-workspace token lookup, and
the streaming response builder — that the loader endpoint imports.
"""

from __future__ import annotations

import logging

import httpx
from fastapi.responses import StreamingResponse

from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

# Only these hosts may be proxied. Anything else is rejected to prevent
# the proxy being abused as an open relay.
# Public so `api.loaders` can import them without violating Python's
# leading-underscore convention (issue #88).
ALLOWED_HOSTS = {
    "files.slack.com",
    "slack-files.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
}

SLACK_HOSTS = {"files.slack.com", "slack-files.com"}

# Runtime-registered hosts derived from active ``PlatformConnection``
# records. Mirrors ``infra.http_safe._runtime_hosts`` for the
# ``/api/media/proxy`` route. Mutated through ``register_runtime_media_hosts``
# so a UI-configured Mattermost server is automatically allow-listed
# without requiring an env var.
_RUNTIME_HOSTS: frozenset[str] = frozenset()


def register_runtime_media_hosts(hosts: "set[str] | frozenset[str]") -> None:
    """Replace the runtime media-proxy host set. Called from server
    lifespan / connection CRUD paths after deriving the hostnames from
    the configured connections."""
    global _RUNTIME_HOSTS
    _RUNTIME_HOSTS = frozenset(h.strip().lower() for h in hosts if h and h.strip())


def clear_runtime_media_hosts() -> None:
    """Reset runtime media hosts. Used by tests for isolation."""
    global _RUNTIME_HOSTS
    _RUNTIME_HOSTS = frozenset()


def effective_allowed_hosts() -> frozenset[str]:
    """Static cloud-host allowlist plus runtime-registered hosts. The
    ``/api/media/proxy`` handler MUST go through this rather than the
    raw ``ALLOWED_HOSTS`` set so self-hosted platform connections work
    end-to-end."""
    return frozenset(ALLOWED_HOSTS) | _RUNTIME_HOSTS


# Content types we are willing to render inline in the browser. Anything
# outside this allowlist (notably ``image/svg+xml`` and ``text/html``, which
# can execute same-origin script) is served ``Content-Disposition: attachment``
# so a malicious uploaded "image" can never run as a same-origin document.
_INLINE_SAFE_TYPES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "image/avif",
        "application/pdf",
    }
)


def safe_media_headers(content_type: str | None) -> dict[str, str]:
    """Return anti-XSS response headers for a proxied media byte-stream.

    Centralizes the hardening both proxies (``/api/files/proxy`` and
    ``/api/media/proxy``) and ``build_response`` apply:

    - ``X-Content-Type-Options: nosniff`` — stop the browser MIME-sniffing a
      served blob into an executable type.
    - ``Content-Security-Policy: default-src 'none'; sandbox`` — even if the
      response is interpreted as a document, it can load nothing and runs in a
      sandbox with no script/origin privileges.
    - ``Content-Disposition`` — ``inline`` only for the known-safe image/PDF
      allowlist; ``attachment`` for everything else (so an SVG/HTML "image"
      downloads instead of executing in the page's origin).
    """
    mime = (content_type or "").split(";", 1)[0].strip().lower()
    disposition = "inline" if mime in _INLINE_SAFE_TYPES else "attachment"
    return {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": disposition,
    }


# httpx client lifetime: one per process. Connection pooling + HTTP/2.
_client: httpx.AsyncClient | None = None


def get_proxy_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            # SSRF defense (CodeQL alerts #37, #38): refuse to follow redirects
            # so an allowlisted host cannot 302 the request to a private IP or
            # off-allowlist target after our pre-fetch validation passes.
            # Slack files.slack.com and Discord CDN signed URLs serve content
            # directly with 200 — they do not redirect in normal operation.
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def slack_bot_tokens() -> list[str]:
    """Return bot tokens for all connected Slack workspaces.

    The proxy tries each in order until one returns 200. This is simpler
    than matching tokens to team IDs embedded in URLs and keeps the proxy
    usable across multi-workspace deployments without extra config.
    """
    stores = get_stores()
    tokens: list[str] = []
    try:
        connections = await stores.platform.list_connections()
    except Exception:
        logger.exception("media proxy: failed to list connections")
        return tokens
    for conn in connections:
        if conn.platform != "slack" or conn.status != "connected":
            continue
        try:
            creds = stores.platform.decrypt_connection_credentials(conn)
        except Exception:
            continue
        token = creds.get("bot_token") or ""
        if token:
            tokens.append(token)
    return tokens


def build_response(upstream: httpx.Response) -> StreamingResponse:
    """Wrap an httpx response as a streaming FastAPI response.

    - Applies the centralized anti-XSS headers (``safe_media_headers``):
      ``nosniff`` + a CSP sandbox + a content-type-aware
      ``Content-Disposition`` (``inline`` only for the known-safe image/PDF
      allowlist, ``attachment`` otherwise — so an SVG/HTML "image" downloads
      instead of executing same-origin).
    - Preserves `Content-Type` and `Content-Length` from upstream.
    - Adds a short private cache to reduce re-fetches during a session.
    """
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    headers: dict[str, str] = {
        "Content-Type": content_type,
        "Cache-Control": "private, max-age=300",
        **safe_media_headers(content_type),
    }
    if "content-length" in upstream.headers:
        headers["Content-Length"] = upstream.headers["content-length"]

    async def _iter():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(_iter(), status_code=200, headers=headers)
