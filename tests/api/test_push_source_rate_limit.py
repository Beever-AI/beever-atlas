"""Per-source rate limit tests for ``POST /api/sources/{source_id}/events``.

Spec: ``openspec/changes/oss-redesign-production-wiring/specs/push-source-ingestion/``

Covers §7 of the production-wiring change: the endpoint MUST rate-limit
per ``source_id`` (NOT client IP) at 60 requests / minute, returning 429
with the documented JSON shape and a ``Retry-After`` header on breach.
Per-source isolation: one source hitting its limit does NOT affect others.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from beever_atlas.infra.rate_limit import limiter
from beever_atlas.models.persistence import ExternalSource
from beever_atlas.server.app import app


_SOURCE_A = "rate-test-source-a"
_SOURCE_B = "rate-test-source-b"
_SECRET = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"


def _sign(secret: str, ts: int, body: bytes) -> str:
    sig = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={sig}"


def _valid_body() -> dict:
    return {
        "channel_id": "C1",
        "channel_name": "general",
        "events": [
            {
                "message_id": "msg-1",
                "timestamp": "2026-04-30T12:00:00Z",
                "author": "U1",
                "author_name": "Alice",
                "content": "x",
            }
        ],
    }


def _make_source(source_id: str) -> ExternalSource:
    return ExternalSource(
        source_id=source_id,
        secret=_SECRET,
        secret_fingerprint=hashlib.sha256(_SECRET.encode()).hexdigest(),
        allowed_channels_pattern="*",
    )


def _signed_request(client: AsyncClient, source_id: str):
    body = _valid_body()
    body_bytes = json.dumps(body).encode("utf-8")
    ts = int(time.time())
    sig = _sign(_SECRET, ts, body_bytes)
    return client.post(
        f"/api/sources/{source_id}/events",
        content=body_bytes,
        headers={"X-Beever-Signature": sig, "Content-Type": "application/json"},
    )


@pytest.fixture
def two_sources(mock_stores):
    """Wire up two registered sources so tests can exercise per-source isolation."""
    sources = {_SOURCE_A: _make_source(_SOURCE_A), _SOURCE_B: _make_source(_SOURCE_B)}

    async def _get_source(sid: str):
        return sources.get(sid)

    mock_stores.mongodb.get_external_source = AsyncMock(side_effect=_get_source)
    mock_stores.mongodb.get_idempotency_record = AsyncMock(return_value=None)
    mock_stores.mongodb.reserve_idempotency_record = AsyncMock(return_value=True)
    mock_stores.mongodb.upsert_channel_messages = AsyncMock(
        return_value={"inserted": 1, "matched": 0}
    )
    return mock_stores


@pytest.fixture(autouse=True)
def _reset_limiter():
    """Drop slowapi's in-memory storage between tests so the per-source
    bucket starts fresh — without this, the limit count leaks across
    tests in arbitrary pytest orderings."""
    try:
        limiter.reset()
    except Exception:
        # storage backends without reset() — rare in tests
        pass
    yield
    try:
        limiter.reset()
    except Exception:
        pass


@pytest.fixture
async def client(two_sources):  # noqa: ARG001
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_within_rate_limit_returns_202(client: AsyncClient) -> None:
    """Spec scenario: 30 valid requests in one minute all return 202."""
    for _ in range(30):
        resp = await _signed_request(client, _SOURCE_A)
        assert resp.status_code == 202, resp.text


@pytest.mark.asyncio
async def test_exceeds_rate_limit_returns_429_with_retry_after(client: AsyncClient) -> None:
    """Spec scenario: 61st request in one minute returns 429 with the
    documented JSON body and a ``Retry-After`` header."""
    # Burn 60 requests
    for _ in range(60):
        resp = await _signed_request(client, _SOURCE_A)
        assert resp.status_code == 202

    # 61st should be rate-limited
    resp = await _signed_request(client, _SOURCE_A)
    assert resp.status_code == 429
    assert "Retry-After" in resp.headers
    body = resp.json()
    assert body["error"] == "rate_limited"
    assert body["source_id"] == _SOURCE_A
    assert isinstance(body["retry_after_seconds"], int)
    assert body["retry_after_seconds"] > 0


@pytest.mark.asyncio
async def test_per_source_isolation(client: AsyncClient, two_sources) -> None:
    """Spec scenario: source A at its limit does NOT affect source B."""
    # Burn through source A's quota
    for _ in range(60):
        resp = await _signed_request(client, _SOURCE_A)
        assert resp.status_code == 202
    # 61st on A → 429
    resp_a = await _signed_request(client, _SOURCE_A)
    assert resp_a.status_code == 429
    # First request on B → still 202 (independent bucket)
    resp_b = await _signed_request(client, _SOURCE_B)
    assert resp_b.status_code == 202

    # The 429 short-circuit MUST NOT mutate stores — verify the upsert
    # was called only for the 60 + 1 successful requests, never for the
    # 429-rejected one.
    upsert_calls = two_sources.mongodb.upsert_channel_messages.await_args_list
    assert len(upsert_calls) == 61
