"""Read-through tests for the durable-media serving proxies.

Covers the ``channel_media_read_through`` branch added to both
``/api/files/proxy`` and ``/api/media/proxy`` in ``api.loaders``: on a blob
store HIT the stored bytes are streamed (with ``X-Media-Source: store``) and
the origin (bridge / upstream CDN) is NOT contacted; on a MISS, a store
error, or with the flag OFF the request falls through to the existing origin
fetch (tagged ``X-Media-Source: origin``).

The blob store is a lightweight fake injected onto the ``StoreClients``
singleton via ``media_blob_store`` so no Mongo/GridFS is required.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Fakes ──────────────────────────────────────────────────────────────────


class _FakeGridOut:
    """Minimal async GridFS-like stream over an in-memory buffer."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._pos = 0
        self.closed = False

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            chunk = self._data[self._pos :]
            self._pos = len(self._data)
            return chunk
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        return chunk

    async def close(self) -> None:
        self.closed = True


class _FakeBlobStore:
    """Fake ``MediaBlobStore`` for the proxy read-through path.

    ``open_by_url`` returns ``(stream, ref)`` on a configured hit, ``None``
    on a miss, or raises if ``raise_on_lookup`` is set (resilience test).
    """

    def __init__(
        self,
        *,
        hit_bytes: bytes | None = None,
        mime_type: str = "image/png",
        raise_on_lookup: bool = False,
    ) -> None:
        self.hit_bytes = hit_bytes
        self.mime_type = mime_type
        self.raise_on_lookup = raise_on_lookup
        self.calls: list[str] = []

    async def open_by_url(self, url: str) -> "tuple[Any, dict] | None":
        self.calls.append(url)
        if self.raise_on_lookup:
            raise RuntimeError("simulated store outage")
        if self.hit_bytes is None:
            return None
        return _FakeGridOut(self.hit_bytes), {"mime_type": self.mime_type}


# ── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_settings():
    """Rebuild the Settings cache around every test in this module.

    The ``files_client`` tests hit the real app, whose loader auth reads
    ``get_settings().api_keys``. ``get_settings()`` is an ``lru_cache``
    singleton: if another test module primed it before conftest's
    ``_auth_bypass`` set ``BEEVER_API_KEYS=test-key`` (e.g. at collection
    time), every request here would 401. Clearing after the conftest autouse
    fixtures (module autouse runs later) guarantees the in-test rebuild sees
    the test env; clearing again on teardown avoids leaking it onward.
    """
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def install_blob_store(monkeypatch):
    """Return a callable that attaches a fake blob store to the stores
    singleton (and forces the read-through flag) for the test duration."""

    def _install(blob_store, *, read_through: bool = True):
        import beever_atlas.stores as stores_mod
        from beever_atlas.api import loaders as loaders_mod

        fake_stores = MagicMock(name="StoreClientsWithBlob")
        fake_stores.media_blob_store = blob_store
        monkeypatch.setattr(loaders_mod, "get_stores", lambda: fake_stores)
        # The shared media helpers also import get_stores; keep them in sync
        # so any incidental call resolves to the same fake.
        monkeypatch.setattr(stores_mod, "get_stores", lambda: fake_stores, raising=False)

        settings = loaders_mod.get_settings()
        monkeypatch.setattr(settings, "channel_media_read_through", read_through)
        return fake_stores

    return _install


@pytest.fixture
def files_client(monkeypatch):
    """TestClient + recorder for ``/api/files/proxy`` with the bridge stubbed.

    Mirrors ``tests/api/test_files_proxy_ssrf.py``: forces a non-mock adapter,
    stubs ``httpx.AsyncClient`` so the bridge call is recorded not networked,
    and stubs ``validate_proxy_url`` so allowlisted URLs reach the body.
    """
    from beever_atlas.api import loaders as loaders_mod
    from beever_atlas.server.app import app

    fake_adapter = MagicMock()
    fake_adapter._client = MagicMock()
    monkeypatch.setattr(loaders_mod, "get_adapter", lambda *a, **kw: fake_adapter)

    recorded: dict[str, str] = {}

    class _FakeResp:
        status_code = 200
        content = b"bridge-bytes"
        headers = {"content-type": "image/gif"}

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url: str, headers=None):  # noqa: ARG002
            recorded["url"] = url
            return _FakeResp()

    monkeypatch.setattr(loaders_mod.httpx, "AsyncClient", _FakeClient)

    def _fake_validate(url, allowlist=None):  # noqa: ARG001
        from urllib.parse import quote

        return quote(url, safe="")

    import beever_atlas.infra.http_safe as http_safe_mod

    monkeypatch.setattr(http_safe_mod, "validate_proxy_url", _fake_validate)
    monkeypatch.setattr(loaders_mod, "validate_proxy_url", _fake_validate, raising=False)

    return TestClient(app), recorded


@pytest.fixture
def media_client():
    """Bare-app TestClient with just the loaders router (no auth)."""
    from beever_atlas.api import loaders

    app = FastAPI()
    app.include_router(loaders.router)
    return TestClient(app)


# ── /api/files/proxy ───────────────────────────────────────────────────────

_SLACK_URL = "https://files.slack.com/files-pri/T1-F1/a.png"


def test_files_proxy_store_hit_streams_stored_bytes(files_client, install_blob_store, auth_headers):
    client, recorded = files_client
    blob = _FakeBlobStore(hit_bytes=b"stored-image-bytes", mime_type="image/png")
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)

    assert r.status_code == 200
    assert r.content == b"stored-image-bytes"
    assert r.headers["X-Media-Source"] == "store"
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["Cache-Control"] == "public, max-age=3600"
    # The bridge must NOT have been called on a store hit.
    assert "url" not in recorded
    assert blob.calls == [_SLACK_URL]


def test_files_proxy_store_miss_falls_through_to_bridge(
    files_client, install_blob_store, auth_headers
):
    client, recorded = files_client
    blob = _FakeBlobStore(hit_bytes=None)
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)

    assert r.status_code == 200
    assert r.content == b"bridge-bytes"
    assert r.headers["X-Media-Source"] == "origin"
    # Bridge WAS called on a miss.
    assert recorded.get("url"), "bridge should be called on a store miss"


def test_files_proxy_store_error_falls_through_to_bridge(
    files_client, install_blob_store, auth_headers
):
    """A store lookup raising must not 500 — fall through to the bridge."""
    client, recorded = files_client
    blob = _FakeBlobStore(raise_on_lookup=True)
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)

    assert r.status_code == 200
    assert r.content == b"bridge-bytes"
    assert r.headers["X-Media-Source"] == "origin"
    assert recorded.get("url"), "bridge should be called when the store errors"


def test_files_proxy_read_through_disabled_skips_store(
    files_client, install_blob_store, auth_headers
):
    """Flag OFF: the store is never consulted even with a stored blob."""
    client, recorded = files_client
    blob = _FakeBlobStore(hit_bytes=b"stored-image-bytes")
    install_blob_store(blob, read_through=False)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)

    assert r.status_code == 200
    assert r.content == b"bridge-bytes"
    assert r.headers["X-Media-Source"] == "origin"
    assert blob.calls == [], "store must not be consulted when read-through is OFF"
    assert recorded.get("url")


# ── /api/media/proxy ───────────────────────────────────────────────────────

_OFF_ALLOWLIST_URL = "https://mattermost.old-self-hosted.example/files/abc/x.png"


def test_media_proxy_store_hit_served_even_off_allowlist(media_client, install_blob_store):
    """A store hit is served BEFORE the host-allowlist check, so an old
    self-hosted host that dropped off the allowlist still serves stored bytes.
    """
    blob = _FakeBlobStore(hit_bytes=b"durable-media", mime_type="image/jpeg")
    install_blob_store(blob)

    r = media_client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})

    assert r.status_code == 200
    assert r.content == b"durable-media"
    assert r.headers["X-Media-Source"] == "store"
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.headers["Cache-Control"] == "private, max-age=300"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Content-Disposition"] == "inline"
    assert blob.calls == [_OFF_ALLOWLIST_URL]


def test_media_proxy_read_through_disabled_rejects_off_allowlist(media_client, install_blob_store):
    """Flag OFF: the store is skipped, so the off-allowlist host is rejected
    by the allowlist check (the legacy behavior) — store never consulted."""
    blob = _FakeBlobStore(hit_bytes=b"durable-media")
    install_blob_store(blob, read_through=False)

    r = media_client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})

    assert r.status_code == 400
    assert "Host not allowed" in r.json()["detail"]
    assert blob.calls == [], "store must not be consulted when read-through is OFF"


def test_media_proxy_store_error_falls_through_to_allowlist(media_client, install_blob_store):
    """A store error on the media path must not 500 — it falls through to the
    normal allowlist/fetch logic (here the off-allowlist host is then 400)."""
    blob = _FakeBlobStore(raise_on_lookup=True)
    install_blob_store(blob)

    r = media_client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})

    assert r.status_code == 400
    assert "Host not allowed" in r.json()["detail"]
