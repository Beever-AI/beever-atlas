"""Tests for the SSRF-safe HTTP helpers."""

from __future__ import annotations

import socket

import pytest

from beever_atlas.infra import http_safe


def _fake_getaddrinfo(ip: str):
    def _inner(host, port, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port))]
    return _inner


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "169.254.169.254",  # AWS/GCP metadata
        "10.0.0.1",
        "192.168.1.10",
        "172.16.5.5",
        "100.64.0.1",
        "0.0.0.0",
    ],
)
def test_resolve_and_validate_rejects_private_v4(monkeypatch, ip):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(ip))
    with pytest.raises(PermissionError):
        http_safe.resolve_and_validate("https://evil.example.com/path")


def test_resolve_and_validate_rejects_private_v6(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("fc00::1"))
    with pytest.raises(PermissionError):
        http_safe.resolve_and_validate("https://evil.example.com/")


def test_resolve_and_validate_rejects_loopback_v6(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("::1"))
    with pytest.raises(PermissionError):
        http_safe.resolve_and_validate("https://evil.example.com/")


def test_resolve_and_validate_accepts_public_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("8.8.8.8"))
    pinned, host = http_safe.resolve_and_validate("https://dns.google/resolve?q=1")
    assert host == "dns.google"
    assert "8.8.8.8" in pinned
    assert pinned.endswith("/resolve?q=1")


def test_resolve_and_validate_allowlist_enforced(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("8.8.8.8"))
    with pytest.raises(PermissionError):
        http_safe.resolve_and_validate(
            "https://dns.google/", allowlist={"other.example"}
        )
    pinned, host = http_safe.resolve_and_validate(
        "https://dns.google/", allowlist={"dns.google"}
    )
    assert host == "dns.google"


def test_resolve_and_validate_rejects_unknown_scheme():
    with pytest.raises(ValueError):
        http_safe.resolve_and_validate("file:///etc/passwd")


def test_resolve_and_validate_rejects_missing_host():
    with pytest.raises(ValueError):
        http_safe.resolve_and_validate("http:///")


@pytest.mark.asyncio
async def test_safe_get_rejects_private_before_request(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))

    class _Boom:
        def __init__(self, *a, **kw):
            raise AssertionError("httpx.AsyncClient must not be constructed")

    monkeypatch.setattr(http_safe.httpx, "AsyncClient", _Boom)
    with pytest.raises(PermissionError):
        await http_safe.safe_get("https://evil.example/")


@pytest.mark.asyncio
async def test_safe_client_disables_redirects(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("8.8.8.8"))

    captured: dict = {}

    class _Client:
        def __init__(self, *, verify, follow_redirects, timeout):
            captured["follow_redirects"] = follow_redirects
            captured["verify"] = verify
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, **kw):
            captured["url"] = url
            captured["headers"] = kw.get("headers", {})

            class _Resp:
                status_code = 200

            return _Resp()

    monkeypatch.setattr(http_safe.httpx, "AsyncClient", _Client)
    await http_safe.safe_get("https://dns.google/path")
    assert captured["follow_redirects"] is False
    assert captured["verify"] is True
    assert captured["headers"].get("Host") == "dns.google"
    assert "8.8.8.8" in captured["url"]
