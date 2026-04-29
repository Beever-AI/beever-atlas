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

    - Forces `Content-Disposition: inline` so browsers display images
      instead of triggering a download.
    - Preserves `Content-Type` and `Content-Length` from upstream.
    - Adds a short private cache to reduce re-fetches during a session.
    """
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    headers: dict[str, str] = {
        "Content-Type": content_type,
        "Content-Disposition": "inline",
        "Cache-Control": "private, max-age=300",
        "X-Content-Type-Options": "nosniff",
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
