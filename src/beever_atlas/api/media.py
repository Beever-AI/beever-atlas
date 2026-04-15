"""Authenticated media proxy for attachments.

Slack `files-pri` URLs and similar endpoints return image bytes only when
the request carries a bearer token (or an active session cookie). The
frontend `<img>` tag can't attach our Slack bot token, so inline thumbnails
never render. This proxy fetches the bytes server-side with the stored
connection credentials and streams them back, letting `<img>` work.

Scope:
- Strict host allowlist (Slack file hosts; Discord CDN is already public).
- Requires an existing Slack connection whose bot_token has read access.
- Streams response with passthrough Content-Type; rewrites Content-Disposition
  to `inline` so the browser renders instead of forcing a download.
- Short private cache (5 min) to avoid re-fetching during a page session.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(tags=["media"])

# Only these hosts may be proxied. Anything else is rejected to prevent
# the proxy being abused as an open relay.
_ALLOWED_HOSTS = {
    "files.slack.com",
    "slack-files.com",
    "cdn.discordapp.com",
    "media.discordapp.net",
}

_SLACK_HOSTS = {"files.slack.com", "slack-files.com"}

# httpx client lifetime: one per process. Connection pooling + HTTP/2.
_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, connect=5.0),
            follow_redirects=True,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _client


async def _slack_bot_tokens() -> list[str]:
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


@router.get("/api/media/proxy")
async def proxy_media(url: str = Query(..., min_length=10, max_length=2048)) -> Response:
    """Fetch an allow-listed media URL with server-side auth and stream it back."""
    try:
        parsed = urlparse(url)
    except Exception:
        raise HTTPException(status_code=400, detail="Malformed URL")

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=400, detail="Only http(s) URLs allowed")

    host = (parsed.hostname or "").lower()
    if host not in _ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail=f"Host not allowed: {host}")

    client = _get_client()
    headers: dict[str, str] = {
        "User-Agent": "BeeverAtlas-MediaProxy/1.0",
        "Accept": "*/*",
    }

    if host in _SLACK_HOSTS:
        tokens = await _slack_bot_tokens()
        if not tokens:
            raise HTTPException(
                status_code=502, detail="No Slack connection available for proxy auth"
            )
        last_status = 0
        for token in tokens:
            try:
                resp = await client.get(
                    url, headers={**headers, "Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError:
                logger.warning("media proxy: slack fetch error for %s", url, exc_info=True)
                continue
            last_status = resp.status_code
            if resp.status_code == 200:
                return _build_response(resp)
            # Close on non-200 so the connection can be reused.
            await resp.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"All Slack tokens failed (last status: {last_status})",
        )

    # Discord CDN and similar: public signed URLs, no auth needed.
    try:
        resp = await client.get(url, headers=headers)
    except httpx.HTTPError:
        logger.warning("media proxy: fetch error for %s", url, exc_info=True)
        raise HTTPException(status_code=502, detail="Upstream fetch failed") from None
    if resp.status_code != 200:
        status = resp.status_code
        await resp.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream returned {status}")
    return _build_response(resp)


def _build_response(upstream: httpx.Response) -> StreamingResponse:
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
