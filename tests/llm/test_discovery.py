"""PR-D: /v1/models discovery for the Add Endpoint form."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from beever_atlas.llm.endpoints import Endpoint, discover_models


def _endpoint(preset: str, base_url: str = "https://api.example.com/v1") -> Endpoint:
    return Endpoint(
        id="ep-test",
        name="test",
        preset=preset,
        base_url=base_url,
        auth_type="api_key",
        encrypted_key=None,
        models=[],
        rpm=500,
    )


def _make_response(*, status: int = 200, payload: Any = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status
    resp.text = "" if payload is None else json.dumps(payload)
    resp.json = MagicMock(return_value=payload or {})
    return resp


@pytest.mark.asyncio
async def test_openai_shape_returns_ids() -> None:
    """``GET {base_url}/models`` returns ``data[].id``."""
    payload = {
        "data": [
            {"id": "gpt-4o-mini", "object": "model"},
            {"id": "gpt-4.1", "object": "model"},
        ]
    }
    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(return_value=_make_response(payload=payload))
        client_cls.return_value = client_instance

        result = await discover_models(_endpoint("openai"))

    assert result["ok"] is True
    assert result["models"] == ["gpt-4o-mini", "gpt-4.1"]


@pytest.mark.asyncio
async def test_ollama_shape_returns_names_and_strips_v1() -> None:
    """Ollama's ``/api/tags`` shape: ``models[].name``. Discovery strips the ``/v1`` suffix."""
    payload = {
        "models": [
            {"name": "gemma3:e4b", "model": "gemma3:e4b"},
            {"name": "qwen2.5:14b", "model": "qwen2.5:14b"},
        ]
    }
    requested_url: dict[str, str] = {}

    async def fake_get(url: str, **_kw: Any) -> MagicMock:
        requested_url["url"] = url
        return _make_response(payload=payload)

    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(side_effect=fake_get)
        client_cls.return_value = client_instance

        result = await discover_models(_endpoint("ollama", "http://localhost:11434/v1"))

    assert result["ok"] is True
    assert result["models"] == ["gemma3:e4b", "qwen2.5:14b"]
    # /v1 suffix was stripped before hitting /api/tags
    assert requested_url["url"] == "http://localhost:11434/api/tags"


@pytest.mark.asyncio
async def test_bedrock_returns_not_supported() -> None:
    """Bedrock + Vertex surface a structured "manual entry" error."""
    result = await discover_models(_endpoint("bedrock", base_url=""))
    assert result["ok"] is False
    assert "discovery_not_supported_for_preset" in result["error"]


@pytest.mark.asyncio
async def test_vertex_returns_not_supported() -> None:
    result = await discover_models(_endpoint("vertex_ai", base_url=""))
    assert result["ok"] is False
    assert "discovery_not_supported_for_preset" in result["error"]


@pytest.mark.asyncio
async def test_empty_base_url_surfaces_clear_error() -> None:
    result = await discover_models(_endpoint("custom", base_url=""))
    assert result["ok"] is False
    assert "discovery_no_base_url" in result["error"]


@pytest.mark.asyncio
async def test_timeout_surfaces_structured_error() -> None:
    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(side_effect=httpx.ReadTimeout("timed out"))
        client_cls.return_value = client_instance

        result = await discover_models(_endpoint("openai"), timeout_seconds=10.0)

    assert result["ok"] is False
    assert "discovery_timeout" in result["error"]
    assert "10.0s" in result["error"]


@pytest.mark.asyncio
async def test_http_error_surfaces_status_code() -> None:
    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(
            return_value=_make_response(status=401, payload={"error": "unauthorized"})
        )
        client_cls.return_value = client_instance

        result = await discover_models(_endpoint("openai"))

    assert result["ok"] is False
    assert "discovery_http_401" in result["error"]


@pytest.mark.asyncio
async def test_invalid_response_shape_returns_error() -> None:
    """Custom URL returns ``{}`` instead of ``{data: [...]}``."""
    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(return_value=_make_response(payload={"foo": "bar"}))
        client_cls.return_value = client_instance

        result = await discover_models(_endpoint("custom"))

    assert result["ok"] is False
    assert "discovery_invalid_response_shape" in result["error"]


@pytest.mark.asyncio
async def test_credential_passed_as_bearer_header() -> None:
    """Plaintext credential becomes ``Authorization: Bearer <key>``."""
    captured_headers: dict[str, dict[str, str]] = {}

    async def fake_get(url: str, *, headers: dict[str, str], **_kw: Any) -> MagicMock:
        captured_headers["headers"] = dict(headers)
        return _make_response(payload={"data": []})

    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(side_effect=fake_get)
        client_cls.return_value = client_instance

        await discover_models(_endpoint("openai"), plaintext_credential="sk-test")

    assert captured_headers["headers"]["Authorization"] == "Bearer sk-test"
