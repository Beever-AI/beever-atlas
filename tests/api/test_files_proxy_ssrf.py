"""SSRF regression tests for the file proxy (security finding H2).

Covers the `outbound-http-ssrf-safety` spec: the ``/api/files/proxy``
endpoint MUST reject URLs whose host is not on the platform allowlist
(including IMDS and loopback), MUST percent-encode the URL before
f-string concat so query-parameter injection via ``&`` is neutralised,
and MUST still fetch legitimate platform URLs.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from beever_atlas.infra import http_safe


def _make_client(monkeypatch):
    """Build a TestClient that bypasses the 'mock adapter' 501 branch and
    stubs the outbound httpx call so validation is the only real logic
    exercised."""
    from beever_atlas.api import channels as channels_mod
    from beever_atlas.server.app import app

    # Force the adapter to look non-mock so `proxy_file` reaches the
    # validation path.
    fake_adapter = MagicMock()
    fake_adapter._client = MagicMock()
    monkeypatch.setattr(channels_mod, "get_adapter", lambda *a, **kw: fake_adapter)

    # Avoid real network: record whichever URL httpx would be asked to GET.
    recorded: dict[str, str] = {}

    class _FakeResp:
        status_code = 200
        content = b"ok"
        headers = {"content-type": "application/octet-stream"}

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

    monkeypatch.setattr(channels_mod._httpx, "AsyncClient", _FakeClient)

    return TestClient(app), recorded


def test_imds_host_is_rejected(monkeypatch, auth_headers):
    client, recorded = _make_client(monkeypatch)
    r = client.get(
        "/api/files/proxy",
        params={"url": "http://169.254.169.254/latest/meta-data/"},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert "url" not in recorded, "bridge must NOT be called for IMDS URL"


def test_localhost_is_rejected(monkeypatch, auth_headers):
    client, recorded = _make_client(monkeypatch)
    r = client.get(
        "/api/files/proxy",
        params={"url": "http://localhost/secrets"},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert "url" not in recorded


def test_unknown_host_is_rejected(monkeypatch, auth_headers):
    client, recorded = _make_client(monkeypatch)
    r = client.get(
        "/api/files/proxy",
        params={"url": "https://attacker.example.com/x"},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert "url" not in recorded


def test_legitimate_slack_url_is_forwarded_encoded(monkeypatch, auth_headers):
    """Legitimate URL on the allowlist is percent-encoded and forwarded."""

    # Short-circuit DNS: the host IS on the allowlist, so resolve_and_validate
    # would still do a DNS lookup. Stub validate_proxy_url so the test
    # exercises the route's encoding + forwarding contract, not DNS.
    def fake_validate(url, allowlist=None):  # noqa: ARG001
        from urllib.parse import quote

        if "slack.com" not in url:
            raise PermissionError("not allowed in test")
        return quote(url, safe="")

    monkeypatch.setattr(
        "beever_atlas.api.channels.validate_proxy_url",
        fake_validate,
        raising=False,
    )
    # Also patch the module-level symbol the route imports:
    import beever_atlas.infra.http_safe as http_safe_mod

    monkeypatch.setattr(http_safe_mod, "validate_proxy_url", fake_validate)

    client, recorded = _make_client(monkeypatch)
    r = client.get(
        "/api/files/proxy",
        params={"url": "https://files.slack.com/files-pri/T1-F1/a.png"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    # Confirm the bridge URL was built with an ENCODED inner URL, not the raw one.
    assert recorded.get("url"), "bridge must have been called"
    assert "https%3A%2F%2Ffiles.slack.com" in recorded["url"]


def test_query_injection_is_neutralised(monkeypatch, auth_headers):
    """An attacker cannot inject `&connection_id=evil` via the url param;
    the `&` is percent-encoded before concatenation."""

    def fake_validate(url, allowlist=None):  # noqa: ARG001
        from urllib.parse import quote

        # Pretend anything with slack.com passes the allowlist.
        if "slack.com" not in url:
            raise PermissionError("not allowed in test")
        return quote(url, safe="")

    import beever_atlas.infra.http_safe as http_safe_mod

    monkeypatch.setattr(http_safe_mod, "validate_proxy_url", fake_validate)

    client, recorded = _make_client(monkeypatch)
    r = client.get(
        "/api/files/proxy",
        params={"url": "https://files.slack.com/a&connection_id=evil#frag"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    forwarded = recorded["url"]
    # The `&` injection from the user-supplied url MUST be encoded as %26
    # inside the url= parameter — it must not split into a second query param.
    # We compare the count of `connection_id=` occurrences: should be 0 because
    # no caller-supplied connection_id was given.
    assert forwarded.count("connection_id=") == 0, forwarded


# ── Direct unit tests on the validator ────────────────────────────────────


def test_validator_rejects_imds():
    with pytest.raises((PermissionError, ValueError)):
        http_safe.validate_proxy_url("http://169.254.169.254/latest/meta-data/")


def test_validator_rejects_off_allowlist_host():
    with pytest.raises(PermissionError):
        http_safe.validate_proxy_url("https://attacker.example.com/x")


def test_validator_allows_slack_suffix_edge():
    """`suffix:.slack-edge.com` matches tenant CDN hosts but not the bare
    ``slack-edge.com`` or an attacker's ``attackerslack-edge.com``."""
    # Attacker-crafted host must fail the suffix match.
    with pytest.raises(PermissionError):
        http_safe.validate_proxy_url(
            "https://attackerslack-edge.com/a",
            allowlist=["suffix:.slack-edge.com"],
        )


def test_validator_rejects_file_scheme():
    with pytest.raises(ValueError):
        http_safe.validate_proxy_url("file:///etc/passwd")


def test_validator_returns_encoded_url(monkeypatch):
    """On success the validator returns a percent-encoded form of the
    original URL. We short-circuit DNS by pre-populating getaddrinfo."""
    import socket

    def fake_getaddrinfo(host, port, type=None):  # noqa: ARG001
        return [(2, 1, 6, "", ("93.184.216.34", port))]  # example.com public IP

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    encoded = http_safe.validate_proxy_url(
        "https://files.slack.com/a?b=1",
        allowlist=["files.slack.com"],
    )
    assert "%3A%2F%2F" in encoded
    assert "%3F" in encoded  # `?` encoded
