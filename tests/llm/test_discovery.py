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
async def test_custom_preset_openai_shape_returns_ids() -> None:
    """``GET {base_url}/models`` returns ``data[].id`` for non-catalog presets.

    PR-ε: commercial presets (openai, anthropic, …) now read the curated
    catalog and never hit upstream. The upstream-``/models`` path is reserved
    for operator-deployed presets (custom, vllm, lmstudio, openrouter,
    litellm_proxy, ollama)."""
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

        result = await discover_models(_endpoint("custom"))

    assert result["ok"] is True
    # ``custom`` doesn't have per-preset classifier rules — the generic
    # name-pattern fallback tags gpt-* as chat. Same kept set, no dropped.
    assert result["models"] == ["gpt-4.1", "gpt-4o-mini"]
    assert result["models_by_kind"]["chat"] == ["gpt-4.1", "gpt-4o-mini"]
    assert result["models_by_kind"]["embedding"] == []
    assert result["dropped"] == {}


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

        result = await discover_models(_endpoint("custom"), timeout_seconds=10.0)

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

        result = await discover_models(_endpoint("custom"))

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
    """Plaintext credential becomes ``Authorization: Bearer <key>``.

    PR-ε: tested via ``custom`` preset since commercial presets now skip
    the HTTP call entirely (catalog path)."""
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

        await discover_models(_endpoint("custom"), plaintext_credential="sk-test")

    assert captured_headers["headers"]["Authorization"] == "Bearer sk-test"


# ─── Security hardening: credential redaction + opt-in SSRF guard ──────────────


def test_redact_credential_fragments() -> None:
    from beever_atlas.llm.endpoints import _redact_credential_fragments

    # Benign text passes through untouched.
    assert _redact_credential_fragments("model not found") == "model not found"
    assert _redact_credential_fragments("503 service unavailable") == "503 service unavailable"
    # Anything that smells like a secret is replaced.
    for tainted in (
        "Authorization: Bearer sk-abc123",
        "invalid api_key supplied",
        '{"error": {"message": "x", "secret": "y"}}',
        "AKIAIOSFODNN7EXAMPLE rejected",
        '"private_key": "-----BEGIN..."',
    ):
        assert _redact_credential_fragments(tainted) == (
            "(redacted — upstream text may contain credential fragments)"
        )


@pytest.mark.asyncio
async def test_discovery_http_error_body_is_redacted_when_secretish() -> None:
    """An upstream error page that echoes the auth header doesn't leak it.

    PR-ε: tested via ``custom`` preset since commercial presets short-circuit
    to the catalog before any HTTP call."""
    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(
            return_value=_make_response(
                status=400, payload={"echoed": "Authorization: Bearer sk-leaked"}
            )
        )
        client_cls.return_value = client_instance

        result = await discover_models(_endpoint("custom"))

    assert result["ok"] is False
    assert "discovery_http_400" in result["error"]
    assert "sk-leaked" not in result["error"]
    assert "redacted" in result["error"]


@pytest.mark.asyncio
async def test_ssrf_guard_blocks_private_target_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the opt-in guard on, a base_url that resolves to a private IP is refused
    BEFORE any outbound request (so the credential never leaves the process)."""
    from types import SimpleNamespace

    import beever_atlas.infra.config as cfg_mod

    monkeypatch.setattr(
        cfg_mod, "get_settings", lambda: SimpleNamespace(llm_endpoint_ssrf_guard=True)
    )

    # If the guard were bypassed, this AsyncMock would record a call.
    called = MagicMock()
    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(side_effect=called)
        client_cls.return_value = client_instance

        result = await discover_models(
            _endpoint("ollama", base_url="http://127.0.0.1:11434/v1"),
            plaintext_credential="sk-should-never-leave",
        )

    assert result["ok"] is False
    assert "discovery_url_blocked" in result["error"]
    called.assert_not_called()


@pytest.mark.asyncio
async def test_ssrf_guard_off_by_default_allows_localhost() -> None:
    """Default (guard off) — the fully-local Ollama preset still works."""
    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(
            return_value=_make_response(payload={"models": [{"name": "gemma3:4b"}]})
        )
        client_cls.return_value = client_instance

        result = await discover_models(_endpoint("ollama", base_url="http://localhost:11434/v1"))

    assert result["ok"] is True
    assert result["models"] == ["gemma3:4b"]


# ─── PR-ε: catalog-discovery returns curated KNOWN_MODELS for commercial presets ─


@pytest.mark.asyncio
async def test_commercial_preset_does_not_hit_upstream() -> None:
    """Discover for a catalog preset (e.g. ``openai``) MUST NOT issue any
    HTTP request — the result comes straight from the curated catalog so
    we can't accidentally pull experimental / live / fine-tune models that
    break Test or dispatch downstream."""
    called = MagicMock()
    with patch("httpx.AsyncClient") as client_cls:
        client_instance = MagicMock()
        client_instance.__aenter__ = AsyncMock(return_value=client_instance)
        client_instance.__aexit__ = AsyncMock(return_value=None)
        client_instance.get = AsyncMock(side_effect=called)
        client_cls.return_value = client_instance

        result = await discover_models(_endpoint("openai"))

    assert result["ok"] is True
    # Catalog-sourced result — no dropped models because we never see anything
    # we wouldn't keep.
    assert result["dropped"] == {}
    # The HTTP client was never invoked.
    called.assert_not_called()


@pytest.mark.asyncio
async def test_jina_catalog_returns_only_embedding_ids() -> None:
    """Jina (embedding-only preset) catalog returns embedding ids only —
    no rerankers, no VLMs, no CLIP, no code-embedding variants that break
    the inference API. Replaces the upstream-``/models`` filter test."""
    result = await discover_models(_endpoint("jina_ai"))
    assert result["ok"] is True
    assert result["models_by_kind"]["chat"] == []
    # Every model in ``models`` is in the embedding bucket; specific ids
    # come from ``llm/known_models.py`` — assert the catalog is non-empty
    # AND every entry is a recognisable jina-embeddings-* id.
    assert result["models"]
    for mid in result["models"]:
        assert mid.startswith("jina-embeddings-"), f"unexpected jina catalog entry: {mid}"
    assert result["dropped"] == {}


@pytest.mark.asyncio
async def test_openai_catalog_separates_chat_and_embedding() -> None:
    """OpenAI catalog populates both chat and embedding buckets."""
    result = await discover_models(_endpoint("openai"))
    assert result["ok"] is True
    assert result["models_by_kind"]["chat"]
    assert result["models_by_kind"]["embedding"]
    # gpt-4o-mini is in the catalog as a chat model.
    assert "gpt-4o-mini" in result["models_by_kind"]["chat"]
    # text-embedding-3-small is in the catalog as an embedding model.
    assert "text-embedding-3-small" in result["models_by_kind"]["embedding"]
    assert result["dropped"] == {}


@pytest.mark.asyncio
async def test_google_ai_catalog_excludes_live_and_image_variants() -> None:
    """The Gemini catalog excludes Live / Image / Realtime / TTS variants
    that 400 on the OpenAI-compat shim — PR-ε's whole reason to exist."""
    result = await discover_models(_endpoint("google_ai"))
    assert result["ok"] is True
    # Every chat entry is a stable Gemini chat id.
    for mid in result["models_by_kind"]["chat"]:
        assert mid.startswith("gemini-"), f"unexpected google_ai catalog entry: {mid}"
        # Critically, NONE of the variants that triggered ``This model only
        # supports Interactions API`` in production.
        assert "-live" not in mid
        assert "-image-generation" not in mid
        assert "-tts" not in mid
        assert "-realtime" not in mid
    assert result["dropped"] == {}
