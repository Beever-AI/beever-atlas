"""Read-through tests for the durable-media serving proxies.

Covers the ``channel_media_read_through`` branch added to both
``/api/files/proxy`` and ``/api/media/proxy`` in ``api.loaders``: on a blob
store HIT the stored bytes are streamed (with ``X-Media-Source: store``) and
the origin (bridge / upstream CDN) is NOT contacted; on a MISS, a store
error, or with the flag OFF the request falls through to the existing origin
fetch (tagged ``X-Media-Source: origin``).

It also covers the hardening added by the durable-media fix sweep:

* S1 — a store hit is gated by ``assert_channel_access(principal, channel_id)``:
  single-tenant / same-owner callers still serve 200, cross-tenant and bridge
  callers get 403, the 403 is NOT swallowed by the store-resilience
  try/except, and the ACL is ordered BEFORE the allowlist 400.
* S2 — both proxies (both branches) carry ``nosniff`` + a CSP sandbox and a
  content-type-aware ``Content-Disposition`` (inline for image/pdf, attachment
  for svg/html).
* S3 — the files-proxy origin body is streamed and an over-cap upstream is
  rejected with 502 before any bytes stream.
* Per-platform ``url_key`` round-trips (Slack/Teams/Mattermost) and store-hit
  ordering for Mattermost- and Teams-shaped off-allowlist hosts.

The blob store is a lightweight fake injected onto the ``StoreClients``
singleton via ``media_blob_store`` so no Mongo/GridFS is required.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.infra.auth import Principal
from beever_atlas.stores.blob_backend import BlobRead
from beever_atlas.stores.media_blob_store import MediaBlobStore

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import AsyncIterator


# ── Fakes ──────────────────────────────────────────────────────────────────


async def _aiter_bytes(data: bytes) -> "AsyncIterator[bytes]":
    """Yield ``data`` as a single-chunk backend-neutral async byte stream."""
    yield data


class _RecordingIterator:
    """Single-chunk async byte stream that records ``aclose`` (C4)."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._done = False
        self.aclosed = False

    def __aiter__(self) -> "_RecordingIterator":
        return self

    async def __anext__(self) -> bytes:
        if self._done:
            raise StopAsyncIteration
        self._done = True
        return self._data

    async def aclose(self) -> None:
        self.aclosed = True


class _FakeBlobStore:
    """Fake ``MediaBlobStore`` for the proxy read-through path.

    The proxy resolves via ``find_refs_for_url`` (ALL candidate refs for a
    url_key, oldest first) + ``open_ref`` (open one ref's blob, ``None`` when
    its bytes are gone). Configure one channel (``channel_id``) or several
    (``channel_ids``) to exercise the per-candidate ACL. ``raise_on_lookup``
    triggers a store outage; ``missing_channels`` marks channels whose ref
    exists but whose blob was purged (``open_ref`` → ``None``).
    """

    def __init__(
        self,
        *,
        hit_bytes: bytes | None = None,
        mime_type: str = "image/png",
        channel_id: str = "C-store",
        channel_ids: "list[str] | None" = None,
        raise_on_lookup: bool = False,
        iterator: "AsyncIterator[bytes] | None" = None,
        missing_channels: "set[str] | None" = None,
    ) -> None:
        self.hit_bytes = hit_bytes
        self.mime_type = mime_type
        # Ordered candidate channels (the real store sorts oldest-first).
        self.channel_ids = channel_ids if channel_ids is not None else [channel_id]
        self.raise_on_lookup = raise_on_lookup
        self._iterator = iterator
        self.missing_channels = missing_channels or set()
        self.calls: list[str] = []

    def _ref(self, url: str, channel_id: str) -> dict:
        return {
            "mime_type": self.mime_type,
            "channel_id": channel_id,
            "sha256": "deadbeef",
            "url_key": MediaBlobStore.normalize_url_key(url),
        }

    async def find_refs_for_url(self, url: str, *, channel_id: str | None = None) -> "list[dict]":
        self.calls.append(url)
        if self.raise_on_lookup:
            raise RuntimeError("simulated store outage")
        if self.hit_bytes is None and self._iterator is None:
            return []
        chans = (
            self.channel_ids
            if channel_id is None
            else [c for c in self.channel_ids if c == channel_id]
        )
        return [self._ref(url, c) for c in chans]

    async def open_ref(self, ref: dict) -> "BlobRead | None":
        if ref["channel_id"] in self.missing_channels:
            return None
        if self._iterator is not None:
            iterator: "AsyncIterator[bytes]" = self._iterator
        else:
            assert self.hit_bytes is not None
            iterator = _aiter_bytes(self.hit_bytes)
        return BlobRead(
            iterator=iterator,
            content_type=self.mime_type,
            size=len(self.hit_bytes) if self.hit_bytes is not None else None,
        )

    async def open_by_url(
        self, url: str, *, channel_id: str | None = None
    ) -> "tuple[BlobRead, dict] | None":
        for ref in await self.find_refs_for_url(url, channel_id=channel_id):
            read = await self.open_ref(ref)
            if read is not None:
                return read, ref
        return None


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
def patch_channel_access(monkeypatch):
    """Return a callable that stubs ``channel_access``'s stores + settings.

    ``assert_channel_access`` binds ``get_stores``/``get_settings`` at module
    import, so the proxy-side ``install_blob_store`` patch on
    ``loaders.get_stores`` does NOT reach it — we patch the channel_access
    module directly. ``connections`` is a list of ``SimpleNamespace`` with
    ``owner_principal_id`` + ``selected_channels``; ``single_tenant`` toggles
    the mode.
    """

    def _patch(connections=None, *, single_tenant: bool = True):
        import beever_atlas.infra.channel_access as ca_mod

        fake_stores = SimpleNamespace(
            platform=SimpleNamespace(list_connections=_async_return(connections or []))
        )
        monkeypatch.setattr(ca_mod, "get_stores", lambda: fake_stores)
        monkeypatch.setattr(
            ca_mod, "get_settings", lambda: SimpleNamespace(beever_single_tenant=single_tenant)
        )

    return _patch


def _async_return(value):
    async def _coro(*_a, **_kw):
        return value

    return _coro


def _conn(owner, selected):
    return SimpleNamespace(owner_principal_id=owner, selected_channels=selected)


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

    The streaming origin branch (S3) is stubbed at the ``client.build_request``
    + ``client.send(stream=True)`` seam so the bridge body is recorded, not
    networked, and ``content-length`` can be set to drive the size-cap test.
    """
    from beever_atlas.api import loaders as loaders_mod
    from beever_atlas.infra.auth import require_user_loader
    from beever_atlas.server.app import app

    fake_adapter = MagicMock()
    fake_adapter._client = MagicMock()
    monkeypatch.setattr(loaders_mod, "get_adapter", lambda *a, **kw: fake_adapter)

    # Override the loader auth so the S1 ACL can be driven from a known
    # principal id (matches ``patch_channel_access`` owners). Real loader auth
    # is exercised by the dedicated auth test modules.
    holder: dict[str, Principal] = {"principal": Principal("user:test", kind="user")}
    saved = app.dependency_overrides.get(require_user_loader)
    app.dependency_overrides[require_user_loader] = lambda: holder["principal"]

    recorded: dict[str, object] = {}

    class _FakeStreamResp:
        def __init__(self, *, status_code=200, body=b"bridge-bytes", headers=None):
            self.status_code = status_code
            self._body = body
            self.headers = headers or {"content-type": "image/gif"}
            self.aclosed = False

        async def aiter_bytes(self):
            # Yield in small chunks so the cap test can trip mid-stream.
            for i in range(0, len(self._body), 4):
                yield self._body[i : i + 4]

        async def aclose(self):
            self.aclosed = True

    class _FakeClient:
        # Default upstream response; tests mutate ``_FakeClient.next_resp``.
        next_resp = None

        def __init__(self, *a, **kw):
            pass

        def build_request(self, method, url, headers=None):  # noqa: ARG002
            recorded["url"] = url
            return SimpleNamespace(method=method, url=url)

        async def send(self, request, stream=False):  # noqa: ARG002
            return _FakeClient.next_resp or _FakeStreamResp()

        async def aclose(self):
            recorded["client_closed"] = True

    monkeypatch.setattr(loaders_mod.httpx, "AsyncClient", _FakeClient)

    def _fake_validate(url, allowlist=None):  # noqa: ARG001
        from urllib.parse import quote

        return quote(url, safe="")

    import beever_atlas.infra.http_safe as http_safe_mod

    monkeypatch.setattr(http_safe_mod, "validate_proxy_url", _fake_validate)
    monkeypatch.setattr(loaders_mod, "validate_proxy_url", _fake_validate, raising=False)

    try:
        yield TestClient(app), recorded, _FakeClient, _FakeStreamResp
    finally:
        if saved is None:
            app.dependency_overrides.pop(require_user_loader, None)
        else:
            app.dependency_overrides[require_user_loader] = saved


@pytest.fixture
def media_client():
    """Bare-app TestClient + a settable principal for the loader router.

    The loader endpoints now declare ``Depends(require_user_loader)`` for S1.
    The bare app has no global auth dependency, so we override that dependency
    to a configurable principal (default: a single-tenant user) — auth itself
    is exercised by the dedicated auth test modules.
    """
    from beever_atlas.api import loaders
    from beever_atlas.infra.auth import require_user_loader

    app = FastAPI()
    app.include_router(loaders.router)

    holder: dict[str, Principal] = {"principal": Principal("user:test", kind="user")}

    def _principal() -> Principal:
        return holder["principal"]

    app.dependency_overrides[require_user_loader] = _principal
    client = TestClient(app)
    return client, holder


# ── /api/files/proxy ───────────────────────────────────────────────────────

_SLACK_URL = "https://files.slack.com/files-pri/T1-F1/a.png"


def test_files_proxy_store_hit_streams_stored_bytes(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    client, recorded, *_ = files_client
    patch_channel_access()  # single-tenant user → allowed
    blob = _FakeBlobStore(hit_bytes=b"stored-image-bytes", mime_type="image/png")
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)

    assert r.status_code == 200
    assert r.content == b"stored-image-bytes"
    assert r.headers["X-Media-Source"] == "store"
    assert r.headers["content-type"].startswith("image/png")
    assert r.headers["Cache-Control"] == "public, max-age=3600"
    # S2: hardening headers present; png is inline.
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in r.headers["Content-Security-Policy"]
    assert r.headers["Content-Disposition"] == "inline"
    # The bridge must NOT have been called on a store hit.
    assert "url" not in recorded
    assert blob.calls == [_SLACK_URL]


def test_files_proxy_store_miss_falls_through_to_bridge(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    client, recorded, *_ = files_client
    patch_channel_access()
    blob = _FakeBlobStore(hit_bytes=None)
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)

    assert r.status_code == 200
    assert r.content == b"bridge-bytes"
    assert r.headers["X-Media-Source"] == "origin"
    # Bridge WAS called on a miss.
    assert recorded.get("url"), "bridge should be called on a store miss"


def test_files_proxy_store_error_falls_through_to_bridge(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """A store lookup raising must not 500 — fall through to the bridge."""
    client, recorded, *_ = files_client
    patch_channel_access()
    blob = _FakeBlobStore(raise_on_lookup=True)
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)

    assert r.status_code == 200
    assert r.content == b"bridge-bytes"
    assert r.headers["X-Media-Source"] == "origin"
    assert recorded.get("url"), "bridge should be called when the store errors"


def test_files_proxy_read_through_disabled_skips_store(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """Flag OFF: the store is never consulted even with a stored blob."""
    client, recorded, *_ = files_client
    patch_channel_access()
    blob = _FakeBlobStore(hit_bytes=b"stored-image-bytes")
    install_blob_store(blob, read_through=False)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)

    assert r.status_code == 200
    assert r.content == b"bridge-bytes"
    assert r.headers["X-Media-Source"] == "origin"
    assert blob.calls == [], "store must not be consulted when read-through is OFF"
    assert recorded.get("url")


# ── /api/files/proxy — S1 channel ACL ───────────────────────────────────────


def test_files_proxy_store_hit_same_owner_serves_200(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """Multi-tenant, caller OWNS a connection listing the ref's channel → 200.

    The mandatory legitimate-access proof: same-owner media still serves.
    """
    client, _recorded, *_ = files_client
    owner = "user:test"  # the files_client require_user_loader override mints this id
    patch_channel_access([_conn(owner, ["C-store"])], single_tenant=False)
    blob = _FakeBlobStore(hit_bytes=b"owned-bytes", channel_id="C-store")
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 200
    assert r.content == b"owned-bytes"
    assert r.headers["X-Media-Source"] == "store"


def test_files_proxy_store_hit_cross_tenant_denied_403(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """Multi-tenant, caller does NOT own the ref's channel → 403, no fallthrough."""
    client, recorded, *_ = files_client
    patch_channel_access([_conn("user:someone-else", ["C-store"])], single_tenant=False)
    blob = _FakeBlobStore(hit_bytes=b"secret", channel_id="C-store")
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "Channel access denied"
    # The 403 must NOT have been swallowed into an origin fetch.
    assert "url" not in recorded, "denied store hit must not fall through to the bridge"


def test_files_proxy_multichannel_serves_via_owned_channel_200(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """Latent-risk fix: a url_key in TWO channels, caller owns only the SECOND.

    The per-candidate ACL denies the first channel and serves via the owned
    second — where the old arbitrary ``find_one`` could have picked the first
    and 403'd media the caller can legitimately see.
    """
    client, _recorded, *_ = files_client
    patch_channel_access([_conn("user:test", ["C-B"])], single_tenant=False)
    blob = _FakeBlobStore(hit_bytes=b"owned-bytes", channel_ids=["C-A", "C-B"])
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 200
    assert r.content == b"owned-bytes"
    assert r.headers["X-Media-Source"] == "store"


def test_files_proxy_multichannel_all_denied_403(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """url_key in two channels, caller owns NEITHER → 403 (security preserved)."""
    client, recorded, *_ = files_client
    patch_channel_access([_conn("user:someone-else", ["C-A", "C-B"])], single_tenant=False)
    blob = _FakeBlobStore(hit_bytes=b"secret", channel_ids=["C-A", "C-B"])
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 403
    assert r.json()["detail"] == "Channel access denied"
    assert "url" not in recorded, "all-denied store hit must not fall through to the bridge"


def test_files_proxy_purged_blob_skips_to_next_candidate_200(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """An authorized candidate whose bytes are gone (purged) is skipped; the
    next authorized+present candidate serves — no false miss, no leak."""
    client, _recorded, *_ = files_client
    patch_channel_access([_conn("user:test", ["C-A", "C-B"])], single_tenant=False)
    blob = _FakeBlobStore(
        hit_bytes=b"present", channel_ids=["C-A", "C-B"], missing_channels={"C-A"}
    )
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 200
    assert r.content == b"present"
    assert r.headers["X-Media-Source"] == "store"


def test_files_proxy_channel_id_hint_scopes_lookup(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """The optional ``channel_id`` restricts the lookup to that exact channel.

    Caller owns C-A but not C-B. Without the hint the URL serves via C-A; with
    ``channel_id=C-B`` the lookup is scoped to C-B (not owned) → 403, proving
    the hint narrows candidates instead of falling back to the owned C-A.
    """
    client, _recorded, *_ = files_client
    patch_channel_access([_conn("user:test", ["C-A"])], single_tenant=False)
    blob = _FakeBlobStore(hit_bytes=b"ab", channel_ids=["C-A", "C-B"])
    install_blob_store(blob)

    r_open = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r_open.status_code == 200, "no hint → served via the owned C-A"

    r_scoped = client.get(
        "/api/files/proxy",
        params={"url": _SLACK_URL, "channel_id": "C-B"},
        headers=auth_headers,
    )
    assert r_scoped.status_code == 403, "channel_id=C-B scopes to C-B only (not owned)"
    assert r_scoped.json()["detail"] == "Channel access denied"


# ── /api/media/proxy ───────────────────────────────────────────────────────

_OFF_ALLOWLIST_URL = "https://mattermost.old-self-hosted.example/files/abc/x.png"
_TEAMS_OFF_ALLOWLIST_URL = (
    "https://graph.microsoft.com/v1.0/drives/D1/items/I1/content?tempauth=zzz"
)


def test_media_proxy_store_hit_served_even_off_allowlist(
    media_client, install_blob_store, patch_channel_access
):
    """A store hit is served BEFORE the host-allowlist check, so an old
    self-hosted host that dropped off the allowlist still serves stored bytes.
    """
    client, _holder = media_client
    patch_channel_access()
    blob = _FakeBlobStore(hit_bytes=b"durable-media", mime_type="image/jpeg")
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})

    assert r.status_code == 200
    assert r.content == b"durable-media"
    assert r.headers["X-Media-Source"] == "store"
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.headers["Cache-Control"] == "private, max-age=300"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in r.headers["Content-Security-Policy"]
    assert r.headers["Content-Disposition"] == "inline"
    assert blob.calls == [_OFF_ALLOWLIST_URL]


@pytest.mark.parametrize("off_url", [_OFF_ALLOWLIST_URL, _TEAMS_OFF_ALLOWLIST_URL])
def test_media_proxy_store_hit_before_allowlist_per_platform(
    media_client, install_blob_store, patch_channel_access, off_url
):
    """Mattermost- and Teams-shaped off-allowlist hosts both serve the store
    hit ahead of the allowlist 400."""
    client, _holder = media_client
    patch_channel_access()
    blob = _FakeBlobStore(hit_bytes=b"x", mime_type="image/png")
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": off_url})
    assert r.status_code == 200
    assert r.headers["X-Media-Source"] == "store"


def test_media_proxy_read_through_disabled_rejects_off_allowlist(
    media_client, install_blob_store, patch_channel_access
):
    """Flag OFF: the store is skipped, so the off-allowlist host is rejected
    by the allowlist check (the legacy behavior) — store never consulted."""
    client, _holder = media_client
    patch_channel_access()
    blob = _FakeBlobStore(hit_bytes=b"durable-media")
    install_blob_store(blob, read_through=False)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})

    assert r.status_code == 400
    assert "Host not allowed" in r.json()["detail"]
    assert blob.calls == [], "store must not be consulted when read-through is OFF"


def test_media_proxy_store_error_falls_through_to_allowlist(
    media_client, install_blob_store, patch_channel_access
):
    """A store error on the media path must not 500 — it falls through to the
    normal allowlist/fetch logic (here the off-allowlist host is then 400)."""
    client, _holder = media_client
    patch_channel_access()
    blob = _FakeBlobStore(raise_on_lookup=True)
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})

    assert r.status_code == 400
    assert "Host not allowed" in r.json()["detail"]


# ── /api/media/proxy — S1 channel ACL + S2 disposition ──────────────────────


def test_media_proxy_store_hit_mcp_principal_allowed(
    media_client, install_blob_store, patch_channel_access
):
    """An mcp principal in single-tenant mode is admitted on a store hit."""
    client, holder = media_client
    holder["principal"] = Principal("mcp:abc123def456ab01", kind="mcp")
    patch_channel_access()
    blob = _FakeBlobStore(hit_bytes=b"m", mime_type="image/png")
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})
    assert r.status_code == 200
    assert r.headers["X-Media-Source"] == "store"


def test_media_proxy_store_hit_cross_tenant_denied_403(
    media_client, install_blob_store, patch_channel_access
):
    """Multi-tenant, caller does not own the channel → 403 on /api/media/proxy."""
    client, _holder = media_client
    patch_channel_access([_conn("user:other", ["C-store"])], single_tenant=False)
    blob = _FakeBlobStore(hit_bytes=b"secret", channel_id="C-store")
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})
    assert r.status_code == 403
    assert r.json()["detail"] == "Channel access denied"


def test_media_proxy_store_hit_bridge_principal_denied_403(
    media_client, install_blob_store, patch_channel_access
):
    """A bridge principal never inherits channel ownership → 403."""
    client, holder = media_client
    holder["principal"] = Principal("bridge", kind="bridge")
    patch_channel_access([_conn("user:other", ["C-store"])], single_tenant=True)
    blob = _FakeBlobStore(hit_bytes=b"secret", channel_id="C-store")
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})
    assert r.status_code == 403
    assert r.json()["detail"] == "Channel access denied"


def test_media_proxy_acl_ordered_before_allowlist(
    media_client, install_blob_store, patch_channel_access
):
    """A denied caller on an OFF-allowlist host gets 403 (ACL), not 400
    (allowlist) — the denied caller must not be able to probe allowlist
    membership via the error code."""
    client, _holder = media_client
    patch_channel_access([_conn("user:other", ["C-store"])], single_tenant=False)
    blob = _FakeBlobStore(hit_bytes=b"secret", channel_id="C-store")
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})
    assert r.status_code == 403  # ACL fires before the allowlist 400


def test_media_proxy_store_hit_svg_forces_attachment(
    media_client, install_blob_store, patch_channel_access
):
    """S2 regression: an SVG store hit is served attachment, not inline."""
    client, _holder = media_client
    patch_channel_access()
    blob = _FakeBlobStore(hit_bytes=b"<svg/>", mime_type="image/svg+xml")
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})
    assert r.status_code == 200
    assert r.headers["Content-Disposition"] == "attachment"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in r.headers["Content-Security-Policy"]


def test_files_proxy_store_hit_html_forces_attachment(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """S2: a text/html store hit on /api/files/proxy is served attachment."""
    client, _recorded, *_ = files_client
    patch_channel_access()
    blob = _FakeBlobStore(hit_bytes=b"<html>", mime_type="text/html")
    install_blob_store(blob)

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["Content-Disposition"] == "attachment"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


# ── /api/files/proxy — S2 origin headers + S3 streaming/size-cap ────────────


def test_files_proxy_origin_carries_safe_headers(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """The origin (bridge) branch carries nosniff + CSP + a safe disposition."""
    client, _recorded, fake_client, fake_resp = files_client
    patch_channel_access()
    install_blob_store(_FakeBlobStore(hit_bytes=None))
    fake_client.next_resp = fake_resp(body=b"origin-image", headers={"content-type": "image/png"})

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 200
    assert r.content == b"origin-image"
    assert r.headers["X-Media-Source"] == "origin"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert "sandbox" in r.headers["Content-Security-Policy"]
    assert r.headers["Content-Disposition"] == "inline"


def test_files_proxy_origin_svg_forces_attachment(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """An origin SVG is served attachment (the stored-XSS vector S2 closes)."""
    client, _recorded, fake_client, fake_resp = files_client
    patch_channel_access()
    install_blob_store(_FakeBlobStore(hit_bytes=None))
    fake_client.next_resp = fake_resp(body=b"<svg/>", headers={"content-type": "image/svg+xml"})

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 200
    assert r.headers["Content-Disposition"] == "attachment"


def test_files_proxy_origin_non_200_maps_to_status(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """A non-200 upstream still maps to HTTPException(status)."""
    client, _recorded, fake_client, fake_resp = files_client
    patch_channel_access()
    install_blob_store(_FakeBlobStore(hit_bytes=None))
    fake_client.next_resp = fake_resp(status_code=404, body=b"")

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 404


def test_files_proxy_origin_over_cap_content_length_502(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """An upstream Content-Length over the cap → 502 BEFORE any bytes stream."""
    client, _recorded, fake_client, fake_resp = files_client
    patch_channel_access()
    install_blob_store(_FakeBlobStore(hit_bytes=None))

    from beever_atlas.api import loaders as loaders_mod

    settings = loaders_mod.get_settings()
    cap = settings.media_max_file_size_mb * 1024 * 1024
    fake_client.next_resp = fake_resp(
        body=b"x",
        headers={"content-type": "image/png", "content-length": str(cap + 1)},
    )

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 502
    assert "too large" in r.json()["detail"].lower()


def test_files_proxy_origin_streams_in_chunks(
    files_client, install_blob_store, patch_channel_access, auth_headers
):
    """The origin body is streamed (multi-chunk) rather than single-buffered."""
    client, _recorded, fake_client, fake_resp = files_client
    patch_channel_access()
    install_blob_store(_FakeBlobStore(hit_bytes=None))
    payload = b"abcdefghijklmnop"  # 16 bytes → 4-byte chunks in the fake
    fake_client.next_resp = fake_resp(body=payload, headers={"content-type": "image/png"})

    r = client.get("/api/files/proxy", params={"url": _SLACK_URL}, headers=auth_headers)
    assert r.status_code == 200
    assert r.content == payload


# ── C4 — inner iterator aclose on a store hit ───────────────────────────────


def test_store_hit_closes_inner_iterator(media_client, install_blob_store, patch_channel_access):
    """``_iter_blob`` awaits the backend iterator's ``aclose`` after streaming."""
    client, _holder = media_client
    patch_channel_access()
    rec = _RecordingIterator(b"durable")
    blob = _FakeBlobStore(iterator=rec, hit_bytes=b"durable", mime_type="image/png")
    install_blob_store(blob)

    r = client.get("/api/media/proxy", params={"url": _OFF_ALLOWLIST_URL})
    assert r.status_code == 200
    assert r.content == b"durable"
    assert rec.aclosed is True


# ── Per-platform url_key round-trips ────────────────────────────────────────

_SLACK_URL_RESIGNED = "https://files.slack.com/files-pri/T1-F1/a.png?t=NEWTOKEN"
_TEAMS_URL = "https://graph.microsoft.com/v1.0/drives/D1/items/I1/content?tempauth=aaa"
_TEAMS_URL_RESIGNED = "https://graph.microsoft.com/v1.0/drives/D1/items/I1/content?tempauth=zzz"
_MM_URL = "https://team.example.com/api/v4/files/fileid123"
_TELEGRAM_URL = "https://api.telegram.org/file/bot123:TOKEN/photos/a.jpg"


def test_url_key_slack_stable_across_resign():
    key = MediaBlobStore.normalize_url_key(_SLACK_URL)
    assert key == "files.slack.com/files-pri/T1-F1/a.png"
    assert MediaBlobStore.normalize_url_key(_SLACK_URL_RESIGNED) == key


def test_url_key_teams_stable_across_resign():
    key = MediaBlobStore.normalize_url_key(_TEAMS_URL)
    assert key == "graph.microsoft.com/v1.0/drives/D1/items/I1/content"
    assert MediaBlobStore.normalize_url_key(_TEAMS_URL_RESIGNED) == key


def test_url_key_mattermost_extensionless_preserved():
    assert MediaBlobStore.normalize_url_key(_MM_URL) == "team.example.com/api/v4/files/fileid123"


def test_url_key_telegram_yields_empty():
    assert MediaBlobStore.normalize_url_key(_TELEGRAM_URL) == ""
