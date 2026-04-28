"""Tests for GET /api/ask/files/{file_id} header sanitization (Fix #1, #11).

Patches the StoreClients singleton's `file_store.open` so these are
pure header-shape assertions with no Mongo dependency.

After issue #31 Phase 3 migration, the endpoint reads the FileStore
from the shared singleton instead of constructing one per request.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

import beever_atlas.stores as stores_mod
from beever_atlas.server.app import app


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def client():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_store_stream(filename: str, owner: str = "user:test") -> MagicMock:
    """Build a mock FileStore.open return that matches the endpoint contract."""
    stream = MagicMock()
    stream.filename = filename
    stream.metadata = {"owner_user_id": owner, "mime_type": "image/png"}
    stream.read = AsyncMock(return_value=b"\x89PNG-bytes")
    return stream


def _install_fake_file_store(stream: MagicMock):
    """Install a fake StoreClients singleton whose `file_store.open` returns
    the given stream. Returns the saved-original so tests can restore."""
    saved = stores_mod._stores
    fake = MagicMock(name="FakeStoreClients")
    fake.file_store.open = AsyncMock(return_value=stream)
    stores_mod._stores = fake
    return saved


@pytest.mark.anyio
@pytest.mark.parametrize(
    "malicious",
    [
        'a"; X-Injected: evil',
        "bad\r\nX-Injected: evil",
        'with"quote.png',
    ],
)
async def test_ask_file_serve_sanitizes_malicious_filename(client, malicious):
    """Malicious filenames must not leak CR/LF or unescaped quotes into headers."""
    saved = _install_fake_file_store(_make_store_stream(malicious))
    try:
        resp = await client.get("/api/ask/files/fake-id")

        assert resp.status_code == 200
        disp = resp.headers["content-disposition"]
        # No raw CR or LF in the header value.
        assert "\r" not in disp
        assert "\n" not in disp
        # The ASCII filename token must contain no stray quote characters.
        # RFC 6266: filename="<ascii>"; filename*=UTF-8''<pct-encoded>
        ascii_part = disp.split(";", 1)[0]  # 'inline; filename="..."'  -> take header dir
        assert ascii_part == "inline"
        # Second segment is the safe_ascii filename="..."
        segments = [s.strip() for s in disp.split(";")]
        ascii_seg = next(s for s in segments if s.startswith("filename="))
        # After stripping the leading filename=" and trailing ", no embedded "
        inner = ascii_seg[len('filename="') : -1]
        assert '"' not in inner
    finally:
        stores_mod._stores = saved


@pytest.mark.anyio
async def test_ask_file_serve_non_ascii_uses_rfc5987(client):
    """Non-ASCII filenames must round-trip via filename*=UTF-8''<pct-encoded>."""
    saved = _install_fake_file_store(_make_store_stream("日本語.png"))
    try:
        resp = await client.get("/api/ask/files/fake-id")

        assert resp.status_code == 200
        disp = resp.headers["content-disposition"]
        assert "filename*=UTF-8''" in disp
        # Percent-encoded bytes for the Japanese characters.
        assert "%E6%97%A5%E6%9C%AC%E8%AA%9E" in disp
    finally:
        stores_mod._stores = saved


@pytest.mark.anyio
async def test_ask_file_serve_nosniff_header_present(client):
    """Every served file response must include X-Content-Type-Options: nosniff."""
    saved = _install_fake_file_store(_make_store_stream("ordinary.png"))
    try:
        resp = await client.get("/api/ask/files/fake-id")

        assert resp.status_code == 200
        assert resp.headers["x-content-type-options"] == "nosniff"
    finally:
        stores_mod._stores = saved


@pytest.mark.anyio
async def test_ask_file_serve_ordinary_ascii_filename(client):
    """Ordinary ASCII filenames stay recognizable inside the RFC 5987 header."""
    saved = _install_fake_file_store(_make_store_stream("report.pdf"))
    try:
        resp = await client.get("/api/ask/files/fake-id")

        assert resp.status_code == 200
        disp = resp.headers["content-disposition"]
        assert 'filename="report.pdf"' in disp
        assert "filename*=UTF-8''report.pdf" in disp
    finally:
        stores_mod._stores = saved
