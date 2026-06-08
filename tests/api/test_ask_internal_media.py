"""Tests for the internal image → text endpoint (bot @mention vision path).

`POST /api/internal/media/extract-text` is mounted behind `require_bridge`, so
a leaked user API key (or no auth at all) must never reach the vision pipeline.
The vision call itself is monkeypatched out — these tests exercise auth, the
size/magic-byte guards, the happy path, and the 4000-char truncation, NOT a
live Gemini call.
"""

from __future__ import annotations

import base64

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient

import beever_atlas.infra.auth as auth_mod
from beever_atlas.api.ask import internal_router
from beever_atlas.infra.auth import require_bridge
from beever_atlas.infra.config import Settings
from beever_atlas.services.media_extractors import ImageExtractor, MediaContent


def _png_bytes() -> bytes:
    # Minimal 1×1 PNG (same fixture used in test_image_extractor_concurrency).
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00"
        b"\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18"
        b"\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )


def _patch_bridge_settings(monkeypatch, **overrides):
    base: dict = dict(
        api_keys="user-key",
        bridge_api_key="s3cret",
        admin_token="admin-tok",
    )
    base.update(overrides)

    def fake_get_settings() -> Settings:
        return Settings(**base)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_mod, "get_settings", fake_get_settings)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(internal_router, dependencies=[Depends(require_bridge)])
    return app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client(monkeypatch):
    _patch_bridge_settings(monkeypatch)
    transport = ASGITransport(app=_build_app())
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


_BRIDGE_HEADERS = {"Authorization": "Bearer s3cret"}


def _body(data: bytes, mime: str = "image/png", message_text: str = "") -> dict:
    return {
        "filename": "shot.png",
        "mime_type": mime,
        "data_b64": base64.b64encode(data).decode("ascii"),
        "message_text": message_text,
    }


@pytest.mark.anyio
async def test_rejects_missing_bridge_auth(client):
    resp = await client.post("/api/internal/media/extract-text", json=_body(_png_bytes()))
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_rejects_user_key(client):
    """A user API key must never satisfy the bridge dependency."""
    resp = await client.post(
        "/api/internal/media/extract-text",
        json=_body(_png_bytes()),
        headers={"Authorization": "Bearer user-key"},
    )
    assert resp.status_code == 401


@pytest.mark.anyio
async def test_oversize_decoded_payload_413(client, monkeypatch):
    import beever_atlas.api.ask as ask_mod

    # Shrink the cap so we don't have to ship 10MB of base64 over the wire.
    monkeypatch.setattr(ask_mod, "MAX_UPLOAD_SIZE", 16)
    big = _png_bytes() + b"\x00" * 64  # > 16 bytes decoded, still a valid PNG header
    resp = await client.post(
        "/api/internal/media/extract-text",
        json=_body(big),
        headers=_BRIDGE_HEADERS,
    )
    assert resp.status_code == 413


@pytest.mark.anyio
async def test_unsupported_mime_415(client):
    resp = await client.post(
        "/api/internal/media/extract-text",
        json=_body(_png_bytes(), mime="application/pdf"),
        headers=_BRIDGE_HEADERS,
    )
    assert resp.status_code == 415


@pytest.mark.anyio
async def test_non_image_magic_bytes_415(client):
    # Supported mime but the bytes are not a real image (poisoned mime_type).
    resp = await client.post(
        "/api/internal/media/extract-text",
        json=_body(b"this is definitely not a png", mime="image/png"),
        headers=_BRIDGE_HEADERS,
    )
    assert resp.status_code == 415


@pytest.mark.anyio
async def test_happy_path_returns_extracted_text(client, monkeypatch):
    async def fake_extract(self, data, filename, metadata=None):  # noqa: ANN001
        return MediaContent(text="A diagram of the deploy pipeline.", media_type="image")

    monkeypatch.setattr(ImageExtractor, "extract", fake_extract)
    resp = await client.post(
        "/api/internal/media/extract-text",
        json=_body(_png_bytes(), message_text="what is this?"),
        headers=_BRIDGE_HEADERS,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["filename"] == "shot.png"
    assert data["mime_type"] == "image/png"
    assert data["extracted_text"] == "A diagram of the deploy pipeline."


@pytest.mark.anyio
async def test_extracted_text_truncated_to_4000(client, monkeypatch):
    async def fake_extract(self, data, filename, metadata=None):  # noqa: ANN001
        return MediaContent(text="x" * 9000, media_type="image")

    monkeypatch.setattr(ImageExtractor, "extract", fake_extract)
    resp = await client.post(
        "/api/internal/media/extract-text",
        json=_body(_png_bytes()),
        headers=_BRIDGE_HEADERS,
    )
    assert resp.status_code == 200
    text = resp.json()["extracted_text"]
    assert text.startswith("x" * 4000)
    assert "truncated" in text
    assert len(text) < 9000
