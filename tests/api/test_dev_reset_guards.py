"""RES-199: /api/dev/reset refuses un-confirmed or non-loopback callers.

The router is only mounted in ``BEEVER_ENV=development`` by the real app, so
these tests build a fresh FastAPI app with just ``dev_router`` attached and
override the admin-token dependency. The assertions focus on the pre-flight
guards — they reject before the destructive stores work is reached, so the
tests do not need a live Neo4j / Mongo / Weaviate stack.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from starlette.types import ASGIApp, Receive, Scope, Send

from beever_atlas.api.dev import _is_loopback_client, router as dev_router
from beever_atlas.infra.auth import require_admin


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def _dev_env(monkeypatch):
    """Force development env + known admin token and Neo4j DB name."""
    monkeypatch.setenv("BEEVER_ENV", "development")
    monkeypatch.setenv("BEEVER_ADMIN_TOKEN", "t-admin")
    monkeypatch.setenv("NEO4J_DATABASE", "neo4j")

    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _build_app() -> FastAPI:
    local = FastAPI()
    local.include_router(dev_router)
    local.dependency_overrides[require_admin] = lambda: "admin"
    return local


@pytest.fixture
async def dev_client(_dev_env):
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.anyio
async def test_missing_confirmation_params_returns_422(dev_client: AsyncClient):
    r = await dev_client.post("/api/dev/reset")
    # Both ``database`` and ``i_understand_data_loss`` are Query(...) required.
    assert r.status_code == 422, r.text


@pytest.mark.anyio
async def test_wrong_database_returns_400(dev_client: AsyncClient):
    r = await dev_client.post(
        "/api/dev/reset",
        params={"database": "not-neo4j", "i_understand_data_loss": "yes"},
    )
    assert r.status_code == 400, r.text
    assert "database" in r.json()["detail"].lower()


@pytest.mark.anyio
async def test_wrong_confirmation_token_returns_400(dev_client: AsyncClient):
    r = await dev_client.post(
        "/api/dev/reset",
        params={"database": "neo4j", "i_understand_data_loss": "maybe"},
    )
    assert r.status_code == 400, r.text
    assert "i_understand_data_loss" in r.json()["detail"]


@pytest.mark.anyio
async def test_non_loopback_client_rejected(_dev_env):
    """Wrap the app in an ASGI middleware that spoofs a public client IP."""

    class _SpoofClient:
        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                scope = dict(scope)
                scope["client"] = ("203.0.113.10", 54321)
            await self.app(scope, receive, send)

    wrapped = _SpoofClient(_build_app())
    transport = ASGITransport(app=wrapped)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            "/api/dev/reset",
            params={"database": "neo4j", "i_understand_data_loss": "yes"},
        )
    assert r.status_code == 403, r.text
    assert "loopback" in r.json()["detail"]


def test_is_loopback_client_unit():
    """Direct unit test of the classifier — doesn't need FastAPI plumbing."""
    from starlette.requests import Request

    def _req(host: str | None) -> Request:
        scope: dict = {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "client": (host, 1234) if host is not None else None,
        }
        return Request(scope)

    assert _is_loopback_client(_req("127.0.0.1"))
    assert _is_loopback_client(_req("::1"))
    assert _is_loopback_client(_req("localhost"))
    assert not _is_loopback_client(_req("10.0.0.5"))
    assert not _is_loopback_client(_req("203.0.113.10"))
    assert not _is_loopback_client(_req(None))
    assert not _is_loopback_client(_req(""))
    assert not _is_loopback_client(_req("garbage"))
