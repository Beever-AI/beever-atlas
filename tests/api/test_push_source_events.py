"""Integration tests for ``POST /api/sources/{source_id}/events`` (PR-D).

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/push-source-ingestion/``

Covers the spec's six requirements:
  1. Endpoint accepts events from external sources (signed batch → 202)
  2. HMAC verification on every push request (skew, missing, invalid)
  3. Idempotency-key replay cache (24h TTL on Mongo side)
  4. Push events land in channel_messages with the source_id
  5. External-sources collection stores per-source secrets (rotation)
  6. Source registration writes correct shape

Tests run against the FastAPI app with the standard mock_stores
fixture — the in-memory store records every upsert / lookup so we can
assert the event lifecycle without a live Mongo container.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.models.persistence import ExternalSource, IdempotencyKeyRecord
from beever_atlas.server.app import app


SOURCE_ID = "openclaw-test"
SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _sign(secret: str, ts: int, body: bytes) -> str:
    sig = hmac.new(
        secret.encode("utf-8"),
        f"{ts}.".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return f"t={ts},v1={sig}"


def _valid_body(channel_id: str = "C1") -> dict:
    return {
        "channel_id": channel_id,
        "channel_name": "general",
        "events": [
            {
                "message_id": "msg-1",
                "timestamp": "2026-04-30T12:00:00Z",
                "author": "U1",
                "author_name": "Alice",
                "content": "hello world",
            },
            {
                "message_id": "msg-2",
                "timestamp": "2026-04-30T12:01:00Z",
                "author": "U2",
                "author_name": "Bob",
                "content": "hi back",
            },
        ],
    }


@pytest.fixture
def configured_source(mock_stores):
    """Wire up a registered ExternalSource on the mock_stores fixture."""
    source = ExternalSource(
        source_id=SOURCE_ID,
        secret=SECRET,
        secret_fingerprint=hashlib.sha256(SECRET.encode()).hexdigest(),
        allowed_channels_pattern="*",
    )
    mock_stores.mongodb.get_external_source = AsyncMock(return_value=source)
    mock_stores.mongodb.get_idempotency_record = AsyncMock(return_value=None)
    mock_stores.mongodb.reserve_idempotency_record = AsyncMock(return_value=True)
    mock_stores.mongodb.upsert_channel_messages = AsyncMock(
        return_value={"inserted": 2, "modified": 0, "matched": 0, "upserted_ids": 2}
    )
    return mock_stores


@pytest.fixture
async def client(configured_source):  # noqa: ARG001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_signed_request_returns_202_with_counters(
    client: AsyncClient, configured_source
) -> None:
    """Spec scenario: ``Valid signed request with batch of events``."""
    payload = _valid_body()
    body_bytes = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 202, resp.text
    data = resp.json()
    assert data["accepted"] == 2
    assert data["channel_id"] == "C1"
    assert data["extraction"] == "queued"
    # Events upserted with the source_id (preserves provenance).
    upsert_calls = configured_source.mongodb.upsert_channel_messages.await_args_list
    rows = upsert_calls[0].args[0]
    assert all(r.source_id == SOURCE_ID for r in rows)


# ---------------------------------------------------------------------------
# HMAC failure modes — all should return 401 with no detail leak
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_signature_returns_401(client: AsyncClient) -> None:
    """Spec scenario: ``Missing signature header``."""
    body_bytes = json.dumps(_valid_body()).encode("utf-8")
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_signature_returns_401(client: AsyncClient) -> None:
    """Spec scenario: ``Invalid signature``."""
    body_bytes = json.dumps(_valid_body()).encode("utf-8")
    ts = int(time.time())
    bad_sig = f"t={ts},v1={'a' * 64}"  # right shape, wrong digest
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": bad_sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_expired_timestamp_returns_401(client: AsyncClient) -> None:
    """Spec scenario: ``Timestamp outside skew window``."""
    body_bytes = json.dumps(_valid_body()).encode("utf-8")
    ts = int(time.time()) - 600  # 10 min ago
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_unknown_source_returns_401(client: AsyncClient, configured_source) -> None:
    """A source_id not in the registry must NOT leak its existence —
    same 401 as a bad signature."""
    configured_source.mongodb.get_external_source = AsyncMock(return_value=None)
    body_bytes = json.dumps(_valid_body()).encode("utf-8")
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        "/api/sources/unknown-source/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_modified_body_after_signing_returns_401(client: AsyncClient) -> None:
    """An attacker can't modify the body and keep a valid signature —
    HMAC covers the body bytes."""
    body_bytes = json.dumps(_valid_body()).encode("utf-8")
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    # Send a different body than what was signed.
    other = json.dumps(_valid_body(channel_id="DIFFERENT_CHANNEL")).encode("utf-8")
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=other,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Idempotency replay cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_with_same_idempotency_key_returns_cached_response(
    client: AsyncClient, configured_source
) -> None:
    """Spec scenario: ``Replay with same idempotency key``."""
    cached = IdempotencyKeyRecord(
        source_id=SOURCE_ID,
        idempotency_key="abc-123",
        response={
            "accepted": 5,
            "deduplicated": 0,
            "channel_id": "C1",
            "extraction": "queued",
        },
    )
    configured_source.mongodb.get_idempotency_record = AsyncMock(return_value=cached)
    body_bytes = json.dumps(_valid_body()).encode("utf-8")
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={
            "X-Beever-Signature": sig,
            "X-Beever-Idempotency-Key": "abc-123",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] == 5
    # Cached path must NOT re-upsert events.
    configured_source.mongodb.upsert_channel_messages.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_request_with_idempotency_key_caches_response(
    client: AsyncClient, configured_source
) -> None:
    body_bytes = json.dumps(_valid_body()).encode("utf-8")
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={
            "X-Beever-Signature": sig,
            "X-Beever-Idempotency-Key": "first-call",
            "Content-Type": "application/json",
        },
    )
    configured_source.mongodb.reserve_idempotency_record.assert_awaited_once()


# ---------------------------------------------------------------------------
# Channel filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_channel_outside_allowed_pattern_returns_403(
    client: AsyncClient, configured_source
) -> None:
    """A source's ``allowed_channels_pattern`` scopes which channels it
    can post to. A glob mismatch is 403 (vs 401 for auth failures)."""
    scoped = ExternalSource(
        source_id=SOURCE_ID,
        secret=SECRET,
        secret_fingerprint=hashlib.sha256(SECRET.encode()).hexdigest(),
        allowed_channels_pattern="prod-*",
    )
    configured_source.mongodb.get_external_source = AsyncMock(return_value=scoped)
    body_bytes = json.dumps(_valid_body(channel_id="staging-channel")).encode("utf-8")
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Body shape
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_json_returns_400(client: AsyncClient) -> None:
    body_bytes = b"not valid json {"
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Code-review L4: 400 must NOT echo the server exception class
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_malformed_body_does_not_leak_exception_class(client: AsyncClient) -> None:
    """The 400 detail used to be ``"Malformed body: JSONDecodeError"`` — that
    leaks the Python exception taxonomy to attackers who hold a valid
    HMAC. Code-review L4: the response must be a generic message; the
    exception class belongs in the server log only."""
    body_bytes = b"not valid json {"
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400
    body = resp.json()
    detail = (body.get("detail") or "").lower()
    assert "malformed" in detail or "invalid" in detail
    for banned in ("jsondecodeerror", "validationerror", "typeerror", "attributeerror"):
        assert banned not in detail, f"response detail leaks Python exception class: {banned}"


# ---------------------------------------------------------------------------
# Code-review M1: payload size caps prevent DoS via giant batches / messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversize_body_returns_413(client: AsyncClient, configured_source) -> None:
    """A request body above 10 MB must be rejected before being fully
    accumulated. Code-review HIGH (second pass): the cap is enforced
    on the actual stream bytes, not just the Content-Length header,
    so chunked-transfer-encoding clients without a Content-Length
    cannot bypass the guard."""
    huge_body = b"x" * 11_000_000  # 11 MB — over the 10 MB cap
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=huge_body,
        headers={
            "X-Beever-Signature": "t=1,v1=abc",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_chunked_transfer_oversize_body_still_rejected(
    client: AsyncClient, configured_source
) -> None:
    """Chunked-transfer-encoding clients have NO Content-Length header,
    so the previous header-only guard let them stream unbounded bytes
    into memory. The streaming cap catches them.

    Note: httpx automatically uses chunked when content is an iterator
    that doesn't expose a length. We simulate by passing a generator.
    """

    async def _chunks():
        for _ in range(110):  # 110 × 100KB = 11 MB
            yield b"x" * 100_000

    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=_chunks(),
        headers={
            "X-Beever-Signature": "t=1,v1=abc",
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 413


@pytest.mark.asyncio
async def test_too_many_events_in_batch_returns_400(client: AsyncClient, configured_source) -> None:
    """PushEventRequest.events caps at 1000 — a 1001-event batch is
    rejected by Pydantic validation."""
    payload = {
        "channel_id": "C1",
        "channel_name": "general",
        "events": [
            {
                "message_id": f"msg-{i}",
                "timestamp": "2026-04-30T12:00:00Z",
                "content": "x",
            }
            for i in range(1001)
        ],
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_oversize_message_content_returns_400(client: AsyncClient, configured_source) -> None:
    """PushEvent.content caps at 100KB — a 200KB content field is rejected."""
    payload = {
        "channel_id": "C1",
        "channel_name": "general",
        "events": [
            {
                "message_id": "huge",
                "timestamp": "2026-04-30T12:00:00Z",
                "content": "x" * 200_000,
            }
        ],
    }
    body_bytes = json.dumps(payload).encode("utf-8")
    ts = int(time.time())
    sig = _sign(SECRET, ts, body_bytes)
    resp = await client.post(
        f"/api/sources/{SOURCE_ID}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Code-review CRITICAL: ExternalSource.secret excluded from model_dump
# ---------------------------------------------------------------------------


def test_external_source_secret_excluded_from_model_dump() -> None:
    """The plaintext HMAC key is required for verification but MUST NOT
    appear in any API response that serializes ``ExternalSource``. The
    ``Field(exclude=True)`` annotation enforces this at the model level
    so a future ``GET /api/sources`` endpoint can't accidentally leak."""
    src = ExternalSource(
        source_id="openclaw-prod",
        secret="THIS-MUST-NOT-LEAK",
        secret_fingerprint="aaa",
    )
    dumped = src.model_dump()
    assert "secret" not in dumped, (
        "ExternalSource.secret must be excluded from model_dump() — Field(exclude=True) regression"
    )
    assert dumped.get("secret_fingerprint") == "aaa"
    assert dumped.get("source_id") == "openclaw-prod"

    json_dump = src.model_dump(mode="json")
    assert "secret" not in json_dump


# ---------------------------------------------------------------------------
# Code-review HIGH: CircuitBreaker concurrent allow() exclusion
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_allow_calls_serialize_through_lock() -> None:
    """Two coroutines racing through ``allow()`` while the breaker is
    in cooldown must result in EXACTLY ONE probe (state advances to
    half_open exactly once, second caller is denied).

    Code-review HIGH guard: the eager-init ``asyncio.Lock`` in
    CircuitBreaker.__init__ is what makes this work. A regression to
    lazy creation would fail under multi-thread invocation; this test
    locks in the asyncio-level serialization at minimum.
    """
    import asyncio
    from unittest.mock import patch

    from beever_atlas.services.circuit_breaker import CircuitBreaker

    breaker = CircuitBreaker(threshold=1, cooldown_seconds=60)
    await breaker.record_failure()
    assert breaker.state() == "open"

    base = __import__("time").monotonic()
    with patch(
        "beever_atlas.services.circuit_breaker.time.monotonic",
        lambda: base + 61,
    ):
        results = await asyncio.gather(breaker.allow(), breaker.allow())
    assert sorted(results) == [False, True]
    assert breaker.state() == "half_open"
