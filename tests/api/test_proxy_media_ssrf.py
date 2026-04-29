"""SSRF regression tests for ``/api/media/proxy`` (CodeQL alerts #37 + #38).

The endpoint accepts a fully attacker-controlled `url` query param. Before
this fix, the only defense was a static `host in ALLOWED_HOSTS` check, which
does not protect against:

  - DNS poisoning / misconfiguration where an allowlisted host (e.g.
    ``files.slack.com``) resolves to ``169.254.169.254`` (cloud metadata)
    or a private RFC1918 address.
  - Post-validation redirect pivot — an allowlisted host returning ``302
    Location: http://127.0.0.1/...`` would have been silently followed
    when the shared httpx client had ``follow_redirects=True``.

The fix:
  1. ``api/media.py:get_proxy_client()`` now sets ``follow_redirects=False``.
  2. ``proxy_media`` runs ``resolve_and_validate(url, ALLOWED_HOSTS)`` before
     fetching, layering DNS + IP-class rejection on top of the static
     allowlist check. The pinned URL is discarded; the original URL is used
     for the fetch so TLS/SNI works normally.

These tests verify those properties without requiring a live network — DNS
resolution is monkeypatched.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ── Fixtures ────────────────────────────────────────────────────────────────


def _fake_getaddrinfo(ip: str):
    """Return a getaddrinfo stub that always resolves to ``ip``."""

    def _inner(host, port, *args, **kwargs):  # noqa: ARG001
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))]

    return _inner


@pytest.fixture
def client_with_loaders(monkeypatch):
    """FastAPI test client with the loaders router mounted, no auth required.

    ``proxy_media`` checks ``adapter`` etc. via FastAPI dependencies. We mount
    the router onto a bare app so a TestClient can call it directly.
    """
    from beever_atlas.api import loaders

    app = FastAPI()
    app.include_router(loaders.router)
    return TestClient(app)


@pytest.fixture
def fake_proxy_client(monkeypatch):
    """Stub ``get_proxy_client`` so no real httpx connection is opened.

    Captures the GET URL and returned 200 with empty bytes by default.
    """
    captured: dict[str, Any] = {"calls": []}

    class _FakeResp:
        def __init__(self, status_code: int = 200, content: bytes = b"x", ctype: str = "image/png"):
            self.status_code = status_code
            self._content = content
            self.headers = {"content-type": ctype}

        async def aiter_bytes(self):
            yield self._content

        async def aclose(self):
            return None

    class _FakeClient:
        async def get(self, url, headers=None):
            captured["calls"].append({"url": url, "headers": dict(headers or {})})
            return _FakeResp()

    fake = _FakeClient()
    from beever_atlas.api import loaders

    monkeypatch.setattr(loaders, "get_proxy_client", lambda: fake)
    return captured


# ── Static allowlist (already in place pre-fix) ────────────────────────────


def test_off_allowlist_host_rejected_400(client_with_loaders, monkeypatch):
    """Pre-existing behavior: unrelated public host returns 400."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("8.8.8.8"))
    resp = client_with_loaders.get(
        "/api/media/proxy", params={"url": "https://attacker.example/secret.png"}
    )
    assert resp.status_code == 400
    assert "Host not allowed" in resp.json()["detail"]


def test_unsupported_scheme_rejected_400(client_with_loaders):
    resp = client_with_loaders.get("/api/media/proxy", params={"url": "ftp://files.slack.com/x"})
    assert resp.status_code == 400
    assert "http(s)" in resp.json()["detail"]


def test_substring_bypass_rejected_400(client_with_loaders):
    """`host in ALLOWED_HOSTS` is exact-match (parsed.hostname), so
    `files.slack.com.evil.com` does not pass."""
    resp = client_with_loaders.get(
        "/api/media/proxy", params={"url": "https://files.slack.com.evil.com/x"}
    )
    assert resp.status_code == 400
    assert "Host not allowed" in resp.json()["detail"]


# ── New behavior: DNS + private-IP rejection (#37, #38) ────────────────────


def test_allowlisted_host_resolving_to_imds_rejected(client_with_loaders, monkeypatch):
    """`files.slack.com` resolving to 169.254.169.254 (cloud metadata) must
    be rejected with the generic 'Invalid media URL' message."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    resp = client_with_loaders.get(
        "/api/media/proxy",
        params={"url": "https://files.slack.com/files-pri/T1-F2/x.png"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid media URL"


def test_allowlisted_host_resolving_to_loopback_rejected(client_with_loaders, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    resp = client_with_loaders.get(
        "/api/media/proxy",
        params={"url": "https://cdn.discordapp.com/attachments/1/2/x.png"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid media URL"


def test_allowlisted_host_resolving_to_rfc1918_rejected(client_with_loaders, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("10.0.0.5"))
    resp = client_with_loaders.get(
        "/api/media/proxy",
        params={"url": "https://media.discordapp.net/x.mp4"},
    )
    assert resp.status_code == 400
    assert resp.json()["detail"] == "Invalid media URL"


def test_dns_failure_is_rejected_as_invalid(client_with_loaders, monkeypatch):
    """getaddrinfo raises (no result) → reject with 400."""

    def _boom(host, port, *args, **kwargs):  # noqa: ARG001
        raise socket.gaierror(socket.EAI_NONAME, "no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    resp = client_with_loaders.get(
        "/api/media/proxy",
        params={"url": "https://files.slack.com/files-pri/T1-F2/x.png"},
    )
    assert resp.status_code == 400


# ── New behavior: redirect-following disabled ──────────────────────────────


def test_proxy_client_disables_follow_redirects():
    """Direct check on the shared httpx client config — `follow_redirects`
    must be False so an allowlisted host cannot 302-pivot the request to a
    private IP or off-allowlist target after our pre-fetch validation."""
    from beever_atlas.api import media

    # Reset the singleton so the test sees the fresh config.
    media._client = None
    client = media.get_proxy_client()
    try:
        assert client.follow_redirects is False
    finally:
        media._client = None  # avoid leaking the test client into other tests


# ── Happy path: validation passes, request reaches the fetch ───────────────


@pytest.mark.asyncio
async def test_allowlisted_host_with_public_ip_proceeds_to_fetch(
    client_with_loaders, fake_proxy_client, monkeypatch
):
    """Discord CDN URL resolving to a public IP passes validation and reaches
    the (stubbed) fetch — no auth header, no token leakage."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("162.159.130.232"))

    resp = client_with_loaders.get(
        "/api/media/proxy",
        params={"url": "https://cdn.discordapp.com/attachments/1/2/x.png"},
    )
    assert resp.status_code == 200
    # Original URL should reach the fetch (NOT an IP-pinned variant) so TLS/SNI
    # works normally.
    assert len(fake_proxy_client["calls"]) == 1
    call = fake_proxy_client["calls"][0]
    assert call["url"] == "https://cdn.discordapp.com/attachments/1/2/x.png"
    assert "Authorization" not in call["headers"]


def test_slack_host_with_public_ip_attaches_bearer_token(
    client_with_loaders, fake_proxy_client, monkeypatch
):
    """`files.slack.com` resolving to public IP gets the Slack bot token in
    the Authorization header. Validates token attachment didn't regress."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("3.5.30.71"))

    async def _fake_tokens():
        return ["xoxb-fake-test-token"]

    from beever_atlas.api import loaders

    monkeypatch.setattr(loaders, "slack_bot_tokens", _fake_tokens)

    resp = client_with_loaders.get(
        "/api/media/proxy",
        params={"url": "https://files.slack.com/files-pri/T1-F2/x.png"},
    )
    assert resp.status_code == 200
    call = fake_proxy_client["calls"][0]
    assert call["headers"].get("Authorization") == "Bearer xoxb-fake-test-token"
    # Original URL preserved — we do NOT pin to IP, so TLS works.
    assert call["url"] == "https://files.slack.com/files-pri/T1-F2/x.png"


def test_slack_host_with_no_tokens_returns_502(client_with_loaders, fake_proxy_client, monkeypatch):
    """Pre-existing behavior preserved: if no Slack workspace is connected,
    proxy_media returns 502 instead of attempting the fetch."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("3.5.30.71"))

    async def _no_tokens():
        return []

    from beever_atlas.api import loaders

    monkeypatch.setattr(loaders, "slack_bot_tokens", _no_tokens)

    resp = client_with_loaders.get(
        "/api/media/proxy",
        params={"url": "https://files.slack.com/files-pri/T1-F2/x.png"},
    )
    assert resp.status_code == 502
    # No fetch should have happened.
    assert fake_proxy_client["calls"] == []
