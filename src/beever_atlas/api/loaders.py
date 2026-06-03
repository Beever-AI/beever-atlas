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
from typing import TYPE_CHECKING
from urllib.parse import quote, urlparse, urlunparse

import httpx
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response, StreamingResponse

from beever_atlas.adapters import get_adapter
from beever_atlas.api.media import (
    SLACK_HOSTS,
    build_response,
    effective_allowed_hosts,
    get_proxy_client,
    slack_bot_tokens,
)
from beever_atlas.infra.config import get_settings
from beever_atlas.infra.http_safe import resolve_and_validate, validate_proxy_url
from beever_atlas.stores import get_stores

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator

logger = logging.getLogger(__name__)

router = APIRouter(tags=["loaders"])

# GridFS read chunk size for the read-through path. Streaming in 256 KB
# slices keeps a large blob (video/PDF) off the heap instead of buffering
# the whole file with a single ``.read()``.
_STORE_CHUNK_BYTES = 256 * 1024


async def _iter_gridfs(stream) -> "AsyncIterator[bytes]":
    """Yield a GridFS download stream in fixed-size chunks, closing it after.

    The blob store hands us an open ``AsyncIOMotorGridOut``; we own closing
    it. Reading in ``_STORE_CHUNK_BYTES`` slices avoids materializing a large
    blob in memory.
    """
    try:
        while True:
            chunk = await stream.read(_STORE_CHUNK_BYTES)
            if not chunk:
                break
            yield chunk
    finally:
        # GridOut.close is async on Motor; guard so a sync/fake stream in
        # tests doesn't crash the response.
        close = getattr(stream, "close", None)
        if close is not None:
            result = close()
            if hasattr(result, "__await__"):
                await result


async def _try_store_hit(url: str, *, cache_control: str, extra_headers: dict[str, str]):
    """Return a ``StreamingResponse`` if the blob store has ``url``, else None.

    Read-through is best-effort: any store error is logged at WARN and
    swallowed so the caller falls through to the origin fetch — the proxy
    must never 500 because the store hiccuped. The raw platform ``url`` is
    used as the lookup key; a hit never fetches it, so passing the
    pre-validation URL here is safe.
    """
    if not get_settings().channel_media_read_through:
        return None
    stores = get_stores()
    blob_store = getattr(stores, "media_blob_store", None)
    if blob_store is None:
        return None
    try:
        hit = await blob_store.open_by_url(url)
    except Exception:
        # Resilience: a store outage must not break serving — fall through
        # to the origin fetch path below.
        logger.warning("media read-through: store lookup failed, falling back", exc_info=True)
        return None
    if hit is None:
        return None
    stream, ref = hit
    headers = {"Cache-Control": cache_control, "X-Media-Source": "store", **extra_headers}
    return StreamingResponse(
        _iter_gridfs(stream),
        media_type=ref.get("mime_type") or "application/octet-stream",
        headers=headers,
    )


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

    # Read-through: serve from the durable blob store before touching the
    # bridge. Validation stays FIRST (above) — `validate_proxy_url` also
    # guards SSRF-logging consistency and the url param shape — but a store
    # hit never fetches the URL, so the lookup sits between validation and
    # the origin fetch. We pass the RAW `url` (not the encoded bridge form)
    # because the store keys on the platform URL's host+path.
    stored = await _try_store_hit(url, cache_control="public, max-age=3600", extra_headers={})
    if stored is not None:
        return stored

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
            headers={"Cache-Control": "public, max-age=3600", "X-Media-Source": "origin"},
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

    # Read-through BEFORE the host-allowlist check (deliberate ordering): the
    # allowlist controls which hosts may be FETCHED, but a store hit never
    # fetches. A self-hosted Mattermost host that has since dropped off the
    # runtime allowlist must still serve its already-stored bytes, so the
    # store lookup precedes the allowlist rejection. The basic scheme check
    # above still runs first to reject non-http(s) garbage before any work.
    stored = await _try_store_hit(
        url,
        cache_control="private, max-age=300",
        extra_headers={
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "inline",
        },
    )
    if stored is not None:
        return stored

    host = (parsed.hostname or "").lower()
    allowed = effective_allowed_hosts()
    if host not in allowed:
        raise HTTPException(status_code=400, detail=f"Host not allowed: {host}")

    # SSRF defense (CodeQL alerts #37, #38): re-validate via DNS resolution +
    # private-IP rejection before sending the bot token (Slack path) or
    # making any outbound request. Without this, an allowlisted host whose
    # DNS resolves to a private IP (DNS poisoning, misconfiguration, IMDS
    # at 169.254.169.254) would let the proxy reach internal targets — the
    # static `effective_allowed_hosts()` check above does not catch that.
    # The pinned URL is intentionally discarded; we use the original URL
    # for the fetch so TLS hostname verification (SNI vs. cert SANs) works
    # normally. The shared client has `follow_redirects=False` so the
    # validated host cannot 302-pivot the request post-validation.
    try:
        resolve_and_validate(url, allowed)
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
                origin = build_response(resp)
                origin.headers["X-Media-Source"] = "origin"
                return origin
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
    origin = build_response(resp)
    origin.headers["X-Media-Source"] = "origin"
    return origin
