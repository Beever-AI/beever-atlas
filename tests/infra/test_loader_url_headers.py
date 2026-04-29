"""Regression tests for issue #35 — LoaderUrlSecurityHeadersMiddleware.

Verifies:
  * Requests carrying ``?access_token=`` get ``Referrer-Policy: no-referrer``
    and ``Cache-Control: no-store`` on the response.
  * Requests WITHOUT ``?access_token=`` are unaffected.
  * Endpoints that set their own ``Cache-Control`` / ``Referrer-Policy``
    keep their values (we use ``setdefault``, not overwrite).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import Response
from fastapi.testclient import TestClient

from beever_atlas.infra.loader_url_headers import LoaderUrlSecurityHeadersMiddleware


def _make_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(LoaderUrlSecurityHeadersMiddleware)

    @app.get("/plain")
    async def plain() -> dict:
        return {"ok": True}

    @app.get("/preset-headers")
    async def preset() -> Response:
        return Response(
            content="{}",
            media_type="application/json",
            headers={
                "Cache-Control": "private, no-store",
                "Referrer-Policy": "strict-origin-when-cross-origin",
            },
        )

    return app


def test_query_string_access_token_adds_security_headers() -> None:
    client = TestClient(_make_app())
    resp = client.get("/plain?access_token=secret123")

    assert resp.status_code == 200
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["cache-control"] == "no-store"


def test_no_access_token_means_no_added_headers() -> None:
    client = TestClient(_make_app())
    resp = client.get("/plain")

    assert resp.status_code == 200
    # The middleware must not touch headers when access_token isn't on the URL.
    assert "referrer-policy" not in resp.headers
    # FastAPI's default JSON responses have no Cache-Control header.
    assert "cache-control" not in resp.headers


def test_endpoint_explicit_headers_are_preserved() -> None:
    """Endpoints that set their own Cache-Control / Referrer-Policy retain
    them — the middleware's setdefault must not override."""
    client = TestClient(_make_app())
    resp = client.get("/preset-headers?access_token=secret123")

    assert resp.status_code == 200
    # Endpoint-set values win.
    assert resp.headers["cache-control"] == "private, no-store"
    assert resp.headers["referrer-policy"] == "strict-origin-when-cross-origin"


def test_unrelated_query_param_does_not_trigger() -> None:
    """Only the literal ``access_token`` query param triggers the middleware,
    not partial matches or other names."""
    client = TestClient(_make_app())
    resp = client.get("/plain?token=secret&page=1")

    assert resp.status_code == 200
    assert "referrer-policy" not in resp.headers
    assert "cache-control" not in resp.headers
