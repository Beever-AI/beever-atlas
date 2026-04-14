"""Tests for the constant-time bridge auth check in connections.py."""

from __future__ import annotations

import hmac
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


@pytest.mark.asyncio
async def test_bridge_auth_accepts_valid_bearer(monkeypatch):
    from beever_atlas.api import connections

    settings = SimpleNamespace(bridge_api_key="s3cret", bridge_hmac_dual=False)
    monkeypatch.setattr(connections, "get_settings", lambda: settings)

    class _Stores:
        class platform:
            @staticmethod
            async def list_connections():
                return []

    monkeypatch.setattr(connections, "get_stores", lambda: _Stores)

    request = SimpleNamespace(headers={"authorization": "Bearer s3cret"})
    result = await connections.list_connections_with_credentials(request)  # type: ignore[arg-type]
    assert result == []


@pytest.mark.asyncio
async def test_bridge_auth_rejects_wrong_key(monkeypatch):
    from beever_atlas.api import connections

    settings = SimpleNamespace(bridge_api_key="s3cret", bridge_hmac_dual=False)
    monkeypatch.setattr(connections, "get_settings", lambda: settings)
    monkeypatch.setattr(connections, "get_stores", lambda: None)

    request = SimpleNamespace(headers={"authorization": "Bearer WRONG"})
    with pytest.raises(HTTPException) as ei:
        await connections.list_connections_with_credentials(request)  # type: ignore[arg-type]
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_bridge_auth_rejects_missing_header(monkeypatch):
    from beever_atlas.api import connections

    settings = SimpleNamespace(bridge_api_key="s3cret", bridge_hmac_dual=False)
    monkeypatch.setattr(connections, "get_settings", lambda: settings)
    monkeypatch.setattr(connections, "get_stores", lambda: None)

    request = SimpleNamespace(headers={})
    with pytest.raises(HTTPException) as ei:
        await connections.list_connections_with_credentials(request)  # type: ignore[arg-type]
    assert ei.value.status_code == 401


@pytest.mark.asyncio
async def test_bridge_auth_uses_compare_digest(monkeypatch):
    """The connections module's hmac.compare_digest is what matches the header."""
    from beever_atlas.api import connections

    calls = {"n": 0}
    original = hmac.compare_digest

    def _spy(a, b):
        calls["n"] += 1
        return original(a, b)

    monkeypatch.setattr(connections.hmac, "compare_digest", _spy)

    settings = SimpleNamespace(bridge_api_key="s3cret", bridge_hmac_dual=False)
    monkeypatch.setattr(connections, "get_settings", lambda: settings)

    class _Stores:
        class platform:
            @staticmethod
            async def list_connections():
                return []

    monkeypatch.setattr(connections, "get_stores", lambda: _Stores)

    request = SimpleNamespace(headers={"authorization": "Bearer s3cret"})
    await connections.list_connections_with_credentials(request)  # type: ignore[arg-type]
    assert calls["n"] >= 1


@pytest.mark.asyncio
async def test_bridge_auth_403_when_key_unconfigured(monkeypatch):
    from beever_atlas.api import connections

    settings = SimpleNamespace(bridge_api_key="", bridge_hmac_dual=False)
    monkeypatch.setattr(connections, "get_settings", lambda: settings)
    monkeypatch.setattr(connections, "get_stores", lambda: None)

    request = SimpleNamespace(headers={"authorization": "Bearer whatever"})
    with pytest.raises(HTTPException) as ei:
        await connections.list_connections_with_credentials(request)  # type: ignore[arg-type]
    assert ei.value.status_code == 403
