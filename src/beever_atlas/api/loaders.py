"""Browser-native loader endpoints — accept ``?access_token=`` query string.

These endpoints serve content consumed by ``<img src>``, ``<a href>``, and
similar browser-native elements that cannot carry custom ``Authorization``
headers. They are mounted in ``server/app.py`` with
``Depends(require_user_loader)`` instead of the standard header-only
``require_user``.

Helpers for the media proxy (host allow-list, multi-workspace token
lookup, streaming response builder) live in ``api.media``; this module
imports them rather than duplicating, so there is one source of truth.
The file proxy uses the bridge instead of fetching directly, so its body
remains self-contained here.

Issue #88 — narrow ``?access_token=`` auth surface to loader endpoints.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlparse, urlunparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from beever_atlas.adapters import get_adapter
from beever_atlas.api.media import (
    ALLOWED_HOSTS,
    SLACK_HOSTS,
    build_response,
    get_proxy_client,
    slack_bot_tokens,
)
from beever_atlas.infra.config import get_settings
from beever_atlas.infra.http_safe import resolve_and_validate, validate_proxy_url

logger = logging.getLogger(__name__)

router = APIRouter(tags=["loaders"])


@router.get("/api/files/proxy")
async def proxy_file(
    url: str = Query(..., description="File URL to proxy"),
    connection_id: str | None = Query(
        None, description="Connection ID for multi-workspace routing"
    ),
):
    """Proxy a file URL through the bridge so the browser ``<img>`` tag
    can render it without seeing bridge credentials."""
    adapter = get_adapter()
    if not hasattr(adapter, "_client"):
        raise HTTPException(status_code=501, detail="File proxy not available in mock mode")

    try:
        encoded_url = validate_proxy_url(url)
    except (PermissionError, ValueError) as exc:
        # Surface a generic message; never echo the attacker-controlled URL.
        # Log the host only so operators can see legitimate misses.
        host = urlparse(url).hostname if url else None
        logger.warning("file_proxy rejected url: host=%s reason=%s", host, type(exc).__name__)
        raise HTTPException(status_code=400, detail="Invalid file URL") from None

    settings = get_settings()
    bridge_url = f"{settings.bridge_url}/bridge/files?url={encoded_url}"
    if connection_id:
        bridge_url += f"&connection_id={quote(connection_id, safe='')}"
    headers: dict[str, str] = {}
    if settings.bridge_api_key:
        headers["Authorization"] = f"Bearer {settings.bridge_api_key}"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.get(bridge_url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail="Failed to fetch file")

        return StreamingResponse(
            iter([resp.content]),
            media_type=resp.headers.get("content-type", "application/octet-stream"),
            headers={"Cache-Control": "public, max-age=3600"},
        )


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
    if host not in ALLOWED_HOSTS:
        raise HTTPException(status_code=400, detail=f"Host not allowed: {host}")

    # SSRF defense (CodeQL alerts #37, #38): re-validate via DNS resolution +
    # private-IP rejection before sending the bot token (Slack path) or
    # making any outbound request. Without this, an allowlisted host whose
    # DNS resolves to a private IP (DNS poisoning, misconfiguration, IMDS
    # at 169.254.169.254) would let the proxy reach internal targets — the
    # static `host in ALLOWED_HOSTS` check above does not catch that.
    # The pinned URL is intentionally discarded; we use the original URL
    # for the fetch so TLS hostname verification (SNI vs. cert SANs) works
    # normally. The shared client has `follow_redirects=False` so the
    # validated host cannot 302-pivot the request post-validation.
    try:
        resolve_and_validate(url, ALLOWED_HOSTS)
    except (PermissionError, ValueError) as exc:
        # Generic message — don't echo attacker-controlled URL or expose
        # whether the failure was DNS-rebinding vs. allowlist-miss vs.
        # private-IP. Operators see the host + reason class in the log.
        logger.warning("media_proxy rejected url: host=%s reason=%s", host, type(exc).__name__)
        raise HTTPException(status_code=400, detail="Invalid media URL") from None

    # Rebuild the request URL from the validated `parsed` components so the
    # value sent to httpx never reuses the raw user-provided string. The
    # scheme/host/path/query/fragment are unchanged in network behaviour
    # (identical bytes on the wire), but the explicit reconstruction acts
    # as a sanitization barrier for CodeQL py/full-ssrf alerts #37 and #38
    # — without it, static analysis follows the raw `url` straight into
    # `client.get(...)` and ignores the host-allowlist + DNS guards above.
    safe_url = urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )

    client = get_proxy_client()
    headers: dict[str, str] = {
        "User-Agent": "BeeverAtlas-MediaProxy/1.0",
        "Accept": "*/*",
    }

    if host in SLACK_HOSTS:
        tokens = await slack_bot_tokens()
        if not tokens:
            raise HTTPException(
                status_code=502, detail="No Slack connection available for proxy auth"
            )
        last_status = 0
        for token in tokens:
            try:
                resp = await client.get(
                    safe_url, headers={**headers, "Authorization": f"Bearer {token}"}
                )
            except httpx.HTTPError:
                logger.warning("media proxy: slack fetch error for host=%s", host, exc_info=True)
                continue
            last_status = resp.status_code
            if resp.status_code == 200:
                return build_response(resp)
            # Close on non-200 so the connection can be reused.
            await resp.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"All Slack tokens failed (last status: {last_status})",
        )

    # Discord CDN and similar: public signed URLs, no auth needed.
    try:
        resp = await client.get(safe_url, headers=headers)
    except httpx.HTTPError:
        logger.warning("media proxy: fetch error for host=%s", host, exc_info=True)
        raise HTTPException(status_code=502, detail="Upstream fetch failed") from None
    if resp.status_code != 200:
        status = resp.status_code
        await resp.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream returned {status}")
    return build_response(resp)
