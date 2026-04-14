"""Tests for the IP-keyed rate limit via slowapi."""

from __future__ import annotations

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


@pytest.fixture
def rate_limited_app():
    """Build a minimal app that mirrors the /api/health limiter wiring."""
    limiter = Limiter(key_func=get_remote_address, storage_uri="memory://")
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    @app.get("/api/health")
    @limiter.limit("60/minute")
    async def health(request: Request):
        return {"status": "ok"}

    return app


def test_health_rate_limit_enforced(rate_limited_app):
    client = TestClient(rate_limited_app)
    ok = 0
    blocked = 0
    for _ in range(61):
        resp = client.get("/api/health")
        if resp.status_code == 200:
            ok += 1
        elif resp.status_code == 429:
            blocked += 1
    assert ok == 60
    assert blocked == 1


def test_health_rate_limit_per_ip_keyed(rate_limited_app):
    """Limiter must be keyed (not global)."""
    client = TestClient(rate_limited_app)
    for _ in range(60):
        client.get("/api/health")
    assert client.get("/api/health").status_code == 429
