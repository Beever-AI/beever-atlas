"""Push-source ingest endpoint.

Lets external agent runtimes (OpenClaw, Hermes Agent) push messages
into Beever Atlas's durable Message Store via:

    POST /api/sources/{source_id}/events
    Headers:
      X-Beever-Signature: t=<unix_ts>,v1=<hex>
      X-Beever-Idempotency-Key: <opaque>   (optional, 24h replay cache)
    Body: {"channel_id": str, "events": [PushEvent...]}

The payload lands in ``channel_messages`` with the registered
``source_id`` (preserves source provenance for queries) and
``extraction_status="pending"`` so the ExtractionWorker picks them up
in the next tick. Returns 202 Accepted with counters for the
sender — it should NOT block on extraction completion.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/push-source-ingestion/``
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from beever_atlas.infra.rate_limit import limiter
from beever_atlas.models.persistence import ChannelMessage
from beever_atlas.services.push_hmac import verify_push_signature
from beever_atlas.stores import get_stores


def _source_id_rate_key(request: Request) -> str:
    """Slowapi ``key_func``: bucket per ``source_id`` path param.

    slowapi's default key_func uses client IP, but a misbehaving OpenClaw
    client might rotate egress IPs while still presenting the same HMAC
    key, so the natural unit of trust + accountability is ``source_id``.
    Falls back to ``"unknown"`` so an early-stage path-mismatch never
    bypasses the limit entirely.
    """
    return str(request.path_params.get("source_id") or "unknown")


logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response shapes
# ---------------------------------------------------------------------------


class PushEvent(BaseModel):
    """One message in a push batch.

    Every variable-length field carries a ``max_length`` cap so a
    compromised source key cannot send a single 1 GB message that
    exhausts API server memory.
    """

    message_id: str = Field(max_length=512)
    """Source-stable identifier — combined with the path's ``source_id``
    and the body's ``channel_id`` forms the dedup key in
    ``channel_messages``."""

    timestamp: datetime
    author: str = Field(default="", max_length=256)
    author_name: str = Field(default="", max_length=256)
    author_image: str = Field(default="", max_length=2048)
    content: str = Field(default="", max_length=100_000)
    thread_id: str | None = Field(default=None, max_length=512)
    attachments: list[dict[str, Any]] = Field(default_factory=list, max_length=64)
    reactions: list[dict[str, Any]] = Field(default_factory=list, max_length=128)
    reply_count: int = Field(default=0, ge=0, le=100_000)
    is_bot: bool = False
    raw_metadata: dict[str, Any] = Field(default_factory=dict)


class PushEventRequest(BaseModel):
    """Body of the POST /api/sources/{source_id}/events request."""

    channel_id: str = Field(max_length=512)
    """Logical channel within the source (the source decides what
    counts as a channel — e.g. an OpenClaw conversation id, a Hermes
    agent session id)."""

    channel_name: str = Field(default="", max_length=512)
    """Optional display label so the UI doesn't have to look up the
    channel by id every time. Defaults to ``channel_id``."""

    events: list[PushEvent] = Field(max_length=1000)
    """Per-batch event cap of 1000 — prevents an unbounded batch from
    blowing up the bulk_write op list. Sources should chunk larger
    uploads."""


class PushEventResponse(BaseModel):
    """202 Accepted body."""

    accepted: int
    deduplicated: int
    channel_id: str
    extraction: str = "queued"


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/api/sources/{source_id}/events",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=PushEventResponse,
)
@limiter.limit("60/minute", key_func=_source_id_rate_key)
async def post_source_events(
    source_id: str,
    request: Request,
    x_beever_signature: str | None = Header(default=None, alias="X-Beever-Signature"),
    x_beever_idempotency_key: str | None = Header(default=None, alias="X-Beever-Idempotency-Key"),
) -> PushEventResponse:
    """Accept a signed batch of push events and queue them for extraction.

    The whole pipeline is intentionally HMAC-only — there's no Bearer
    token auth here because the source is a server-to-server peer that
    doesn't have a user principal. Auth is the per-source HMAC secret.

    Replay protection has two layers:
      1. ``±5 min`` timestamp skew window from the signature header.
      2. ``X-Beever-Idempotency-Key`` 24h replay cache (optional but
         strongly encouraged for retries).
    """
    stores = get_stores()
    # Hard memory cap on the request body. Reads chunks via
    # ``request.stream()`` and bails the moment the accumulated body
    # exceeds 10 MB. Catches:
    #   * Honest clients with Content-Length > 10 MB (we'd reject the
    #     header but Starlette has already started buffering when the
    #     dependency runs).
    #   * Chunked-transfer-encoding clients with no Content-Length
    #     (a header-only check would miss these entirely).
    #   * Lying clients that send Content-Length=5 MB then 50 MB body.
    # Per-field max_length caps on PushEvent are still enforced by
    # Pydantic — this is the outer guard.
    MAX_BODY_BYTES = 10_000_000
    body_buf = bytearray()
    async for chunk in request.stream():
        body_buf.extend(chunk)
        if len(body_buf) > MAX_BODY_BYTES:
            logger.warning(
                "push_body_too_large source_id=%s bytes_seen=%d",
                source_id,
                len(body_buf),
            )
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail="Payload too large",
            )
    body = bytes(body_buf)

    # Look up the source's HMAC secret. ``ExternalSource.secret`` is
    # the plaintext signing key (HMAC verification mathematically needs
    # it); ``secret_fingerprint`` is the sha256 hex shown to operators
    # for rotation observability. Plaintext storage is a documented OSS
    # tradeoff — enterprise KMS integration is a separate path that
    # swaps the secret resolver, not the verifier.
    source = await stores.mongodb.get_external_source(source_id)
    if source is None:
        # Generic 401 — do not leak whether the source exists.
        logger.warning("push_signature_rejected source_id=%s reason=unknown_source", source_id)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    result = verify_push_signature(x_beever_signature or "", body, source.secret)
    if not result.ok:
        logger.warning(
            "push_signature_rejected source_id=%s reason=%s",
            source_id,
            result.reason,
        )
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature")

    # Idempotency replay cache lookup BEFORE upserting events.
    if x_beever_idempotency_key:
        cached = await stores.mongodb.get_idempotency_record(source_id, x_beever_idempotency_key)
        if cached is not None:
            logger.info(
                "push_idempotent_replay source_id=%s key=%s",
                source_id,
                x_beever_idempotency_key,
            )
            return PushEventResponse(**cached.response)

    # Validate body. (FastAPI does this implicitly when we pass the
    # body as a Pydantic param, but here we sign over raw bytes so we
    # need to read+parse manually.)
    try:
        import json

        payload = PushEventRequest.model_validate(json.loads(body))
    except Exception as exc:  # noqa: BLE001
        # Don't echo the exception class to the response — avoids leaking
        # the server-side exception taxonomy to attackers who hold a valid
        # HMAC key.
        logger.warning(
            "push_body_rejected source_id=%s reason=%s",
            source_id,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Malformed request body",
        ) from exc

    # Enforce the source's allowed_channels_pattern. ``*`` accepts all.
    pattern = source.allowed_channels_pattern or "*"
    if pattern != "*":
        import fnmatch

        if not fnmatch.fnmatch(payload.channel_id, pattern):
            logger.warning(
                "push_channel_rejected source_id=%s channel_id=%s pattern=%s",
                source_id,
                payload.channel_id,
                pattern,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="channel_id not allowed for this source",
            )

    # Convert each PushEvent to a ChannelMessage and bulk-upsert. The
    # unique compound index on (source_id, channel_id, message_id) gives
    # idempotency for free — re-delivery of the same message_id is a no-op.
    rows: list[ChannelMessage] = []
    channel_name = payload.channel_name or payload.channel_id
    for ev in payload.events:
        rows.append(
            ChannelMessage(
                source_id=source_id,
                channel_id=payload.channel_id,
                channel_name=channel_name,
                message_id=ev.message_id,
                timestamp=ev.timestamp,
                author=ev.author,
                author_name=ev.author_name,
                author_image=ev.author_image,
                content=ev.content,
                thread_id=ev.thread_id,
                attachments=ev.attachments,
                reactions=ev.reactions,
                reply_count=ev.reply_count,
                is_bot=ev.is_bot,
                raw_metadata=ev.raw_metadata,
                # extraction_status defaults to "pending" — worker handles it.
            )
        )

    upsert_result = await stores.mongodb.upsert_channel_messages(rows)
    accepted = int(upsert_result.get("inserted", 0))
    deduplicated = int(upsert_result.get("matched", 0))

    response = PushEventResponse(
        accepted=accepted,
        deduplicated=deduplicated,
        channel_id=payload.channel_id,
        extraction="queued",
    )

    # Cache the response for the idempotency window.
    if x_beever_idempotency_key:
        await stores.mongodb.reserve_idempotency_record(
            source_id,
            x_beever_idempotency_key,
            response.model_dump(mode="json"),
        )

    logger.info(
        "push_events_accepted source_id=%s channel=%s accepted=%d deduped=%d",
        source_id,
        payload.channel_id,
        accepted,
        deduplicated,
    )
    return response
