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
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from beever_atlas.adapters import get_adapter
from beever_atlas.api.media import (
    SLACK_HOSTS,
    build_response,
    effective_allowed_hosts,
    get_proxy_client,
    safe_media_headers,
    slack_bot_tokens,
)
from beever_atlas.infra.auth import Principal, require_user_loader
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.infra.config import get_settings
from beever_atlas.infra.http_safe import resolve_and_validate, validate_proxy_url
from beever_atlas.stores import get_stores

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator

    from beever_atlas.stores.blob_backend import BlobRead

logger = logging.getLogger(__name__)

router = APIRouter(tags=["loaders"])


async def _iter_blob(read: "BlobRead") -> "AsyncIterator[bytes]":
    """Yield a stored blob's bytes from its backend-neutral async iterator.

    The :class:`BlobRead` the blob store hands us already owns chunking and
    connection/handle release (the backend closes its GridOut or releases its
    S3 connection in the iterator's ``finally`` / ``async with`` exit), so this
    helper just relays the chunks — no backend-specific ``read``/``close``
    duck-typing remains.

    Defense in depth (C4): we also ``aclose()`` the inner backend iterator in
    our own ``finally`` so a GridFS cursor / aiohttp connection is released
    promptly even if the client disconnects mid-stream or a chunk relay raises
    — the backend iterators self-close too, but this guarantees release at the
    relay boundary. The ``getattr`` guard tolerates iterators (e.g. test
    stubs) that don't expose ``aclose``.
    """
    try:
        async for chunk in read.iterator:
            yield chunk
    finally:
        aclose = getattr(read.iterator, "aclose", None)
        if aclose is not None:
            await aclose()


async def _try_store_hit(
    url: str,
    *,
    principal: Principal,
    cache_control: str,
    extra_headers: dict[str, str] | None = None,
    channel_id: str | None = None,
):
    """Return a ``StreamingResponse`` if the blob store has ``url``, else None.

    Read-through is best-effort: any store error is logged at WARN and
    swallowed so the caller falls through to the origin fetch — the proxy
    must never 500 because the store hiccuped. The raw platform ``url`` is
    used as the lookup key; a hit never fetches it, so passing the
    pre-validation URL here is safe.

    Channel ACL (S1), per-candidate: a ``url_key`` (host+path) can legitimately
    exist in more than one channel, so we fetch ALL candidate refs (deterministic,
    oldest first) and authorize each against the authenticated ``principal``,
    serving the FIRST channel it may access. This both closes the IDOR (a caller
    must own a channel that holds the URL) and avoids a spurious 403 when an
    arbitrary ``find_one`` would have picked a channel the caller can't see while
    another holds the same URL. Pass ``channel_id`` (when the caller knows it) for
    an exact single-channel lookup. ``assert_channel_access`` raises
    ``HTTPException(403)``; that deny is the deliberate path and must NOT be
    swallowed into an origin re-fetch — so only an ALL-denied candidate set raises
    403, while refs-exist-but-bytes-gone falls through to ``None`` (origin). The
    store-MISS / store-ERROR / flag-OFF paths all return ``None`` first, preserving
    the resilience contract.
    """
    if not get_settings().channel_media_read_through:
        return None
    stores = get_stores()
    blob_store = getattr(stores, "media_blob_store", None)
    if blob_store is None:
        return None
    try:
        candidates = await blob_store.find_refs_for_url(url, channel_id=channel_id)
    except Exception:
        # Resilience: a store outage must not break serving — fall through
        # to the origin fetch path below.
        logger.warning("media read-through: store lookup failed, falling back", exc_info=True)
        return None
    if not candidates:
        return None

    denied = False
    for ref in candidates:
        try:
            await assert_channel_access(principal, ref["channel_id"])
        except HTTPException as exc:
            # A 403 on one candidate must not hide media the caller can reach
            # via another channel holding the same URL — try the next. Non-403
            # HTTPExceptions propagate unchanged.
            if exc.status_code == 403:
                denied = True
                continue
            raise
        try:
            read = await blob_store.open_ref(ref)
        except Exception:
            # Resilience: a backend hiccup on one candidate → skip it.
            logger.warning(
                "media read-through: open failed channel=%s, skipping",
                ref.get("channel_id"),
                exc_info=True,
            )
            continue
        if read is None:
            # Bytes purged after the ref was read — try the next candidate.
            continue

        media_type = (
            getattr(read, "content_type", None)
            or ref.get("mime_type")
            or "application/octet-stream"
        )
        # S2 anti-XSS hardening, keyed on the RESOLVED media type — nosniff + CSP
        # sandbox + a content-type-aware Content-Disposition (inline only for the
        # known-safe image/PDF allowlist, attachment for SVG/HTML and friends).
        headers = {
            "Cache-Control": cache_control,
            "X-Media-Source": "store",
            **safe_media_headers(media_type),
            **(extra_headers or {}),
        }
        return StreamingResponse(_iter_blob(read), media_type=media_type, headers=headers)

    if denied:
        # The media exists but the caller can access NO channel that holds it.
        raise HTTPException(status_code=403, detail="Channel access denied")
    # Candidates existed but their bytes are gone → behave as a miss (origin).
    return None


@router.get("/api/files/proxy")
async def proxy_file(
    url: str = Query(..., description="File URL to proxy"),
    connection_id: str | None = Query(
        None, description="Connection ID for multi-workspace routing"
    ),
    channel_id: str | None = Query(
        None, description="Channel the media is being viewed in (exact store lookup + ACL)"
    ),
    principal: Principal = Depends(require_user_loader),
):
    """Proxy a file URL through the bridge so the browser ``<img>`` tag
    can render it without seeing bridge credentials.

    The router already mounts this under ``Depends(require_user_loader)``;
    declaring ``principal`` here re-uses that same (deduped) dependency so we
    can authorize a store hit against its channel (S1) — FastAPI runs the
    dependency once.
    """
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
    stored = await _try_store_hit(
        url,
        principal=principal,
        cache_control="public, max-age=3600",
        channel_id=channel_id,
    )
    if stored is not None:
        return stored

    settings = get_settings()
    bridge_url = f"{settings.bridge_url}/bridge/files?url={encoded_url}"
    if connection_id:
        bridge_url += f"&connection_id={quote(connection_id, safe='')}"
    headers: dict[str, str] = {}
    if settings.bridge_api_key:
        headers["Authorization"] = f"Bearer {settings.bridge_api_key}"

    # S3: stream the upstream body instead of buffering it into memory. The
    # client + response are kept open across the response lifetime and closed
    # via a BackgroundTask once Starlette finishes streaming. S2: the origin
    # branch gets the same nosniff + CSP-sandbox + safe-disposition headers as
    # the store-hit branch.
    max_bytes = settings.media_max_file_size_mb * 1024 * 1024
    client = httpx.AsyncClient(timeout=30.0)
    try:
        req = client.build_request("GET", bridge_url, headers=headers)
        resp = await client.send(req, stream=True)
    except BaseException:
        await client.aclose()
        raise

    if resp.status_code != 200:
        status = resp.status_code
        await resp.aclose()
        await client.aclose()
        raise HTTPException(status_code=status, detail="Failed to fetch file")

    # Reject an over-cap upstream BEFORE streaming a single byte when the
    # Content-Length advertises it; chunked/unknown-length responses are
    # capped mid-stream below.
    content_length = resp.headers.get("content-length")
    if content_length is not None:
        try:
            declared = int(content_length)
        except ValueError:
            declared = -1
        if declared > max_bytes:
            await resp.aclose()
            await client.aclose()
            raise HTTPException(status_code=502, detail="Upstream file too large")

    content_type = resp.headers.get("content-type", "application/octet-stream")

    async def _iter_origin() -> "AsyncIterator[bytes]":
        streamed = 0
        async for chunk in resp.aiter_bytes():
            streamed += len(chunk)
            if streamed > max_bytes:
                # Abort an over-cap chunked stream mid-flight. The response is
                # closed by the BackgroundTask; raising here truncates the body.
                raise HTTPException(status_code=502, detail="Upstream file too large")
            yield chunk

    async def _close() -> None:
        await resp.aclose()
        await client.aclose()

    response_headers = {
        "Cache-Control": "public, max-age=3600",
        "X-Media-Source": "origin",
        **safe_media_headers(content_type),
    }
    return StreamingResponse(
        _iter_origin(),
        media_type=content_type,
        headers=response_headers,
        background=BackgroundTask(_close),
    )


@router.get("/api/media/proxy")
async def proxy_media(
    url: str = Query(..., min_length=10, max_length=2048),
    channel_id: str | None = Query(
        None, description="Channel the media is being viewed in (exact store lookup + ACL)"
    ),
    principal: Principal = Depends(require_user_loader),
) -> Response:
    """Fetch an allow-listed media URL with server-side auth and stream it back.

    ``principal`` re-uses the router-level ``require_user_loader`` dependency
    (deduped by FastAPI) so a store hit can be authorized against its channel
    (S1).
    """
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
    #
    # S1 ordering note: the channel-ACL check inside ``_try_store_hit`` now
    # gates the store hit, so a caller who cannot access the ref's channel
    # gets a 403 BEFORE the allowlist's 400 — intended. A cross-tenant caller
    # must be denied regardless of allowlist state and must not be able to
    # probe allowlist membership via the error code. The anti-XSS headers
    # (nosniff + CSP sandbox + content-type-aware disposition) are applied
    # inside ``_try_store_hit`` keyed on the resolved media type — an SVG hit
    # is served ``attachment``, not the prior always-``inline``.
    stored = await _try_store_hit(
        url,
        principal=principal,
        cache_control="private, max-age=300",
        channel_id=channel_id,
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
