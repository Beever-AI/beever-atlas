"""Tests for the provider-agnostic embedding shim (PR-A).

Covers:
  * Chunking — 250 inputs → 3 LiteLLM calls of size [100, 100, 50].
  * Vector ordering preserved across chunk boundaries.
  * Retry on 429 then success.
  * Retry budget exhausted → raise.
  * Length-mismatch in provider response → ``EmbeddingResponseError``.
  * Unknown provider prefix → ``EmbeddingProviderError`` at first call.
  * ``JINA_API_KEY`` → ``JINA_AI_API_KEY`` bridge does NOT overwrite when target set.
  * ``task=`` kwarg flows for Jina, dropped for OpenAI (via known-models table).
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest

from beever_atlas.infra.config import Settings
from beever_atlas.llm import embeddings as emb


# ─── Helpers ───────────────────────────────────────────────────────────────


def _make_settings(**overrides: Any) -> Settings:
    base = {
        "embedding_provider": "jina_ai",
        "embedding_model": "jina-embeddings-v4",
        "embedding_dimensions": 2048,
        "embedding_rpm": 500,
        "embedding_api_base": "",
        "embedding_api_key": "test-key",
        "embedding_task": "text-matching",
    }
    base.update(overrides)
    return Settings(**base)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Reset module state and isolate the legacy ``JINA_*`` env vars.

    The legacy alias bridge in ``Settings._bridge_legacy_jina_aliases`` uses
    ``os.environ`` to decide whether to copy ``JINA_*`` values into the new
    ``EMBEDDING_*`` fields. Without this fixture a developer's ``.env``-loaded
    ``JINA_API_URL`` / ``JINA_MODEL`` would leak into Settings instances
    constructed from kwargs, clobbering the explicit values the test set.
    """
    for var in ("JINA_API_URL", "JINA_MODEL", "JINA_DIMENSIONS", "JINA_RPM"):
        monkeypatch.delenv(var, raising=False)
    for var in (
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_RPM",
        "EMBEDDING_API_BASE",
        "EMBEDDING_API_KEY",
        "EMBEDDING_TASK",
    ):
        monkeypatch.delenv(var, raising=False)
    emb._runtime_initialised = False
    yield
    emb._runtime_initialised = False


def _vec(dim: int = 4) -> list[float]:
    return [0.0] * dim


# ─── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chunking_250_inputs_yields_three_calls(monkeypatch):
    """250 inputs → 3 chunks of size [100, 100, 50], vectors returned in order."""
    captured_chunks: list[list[str]] = []

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        captured_chunks.append(list(chunk))
        return [_vec() for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    settings = _make_settings()

    inputs = [f"text-{i}" for i in range(250)]
    out = await emb.embed_texts(inputs, settings=settings)

    assert len(captured_chunks) == 3
    assert [len(c) for c in captured_chunks] == [100, 100, 50]
    assert len(out) == 250
    # Verify chunk concatenation matches the original input order.
    flat = [t for c in captured_chunks for t in c]
    assert flat == inputs


@pytest.mark.asyncio
async def test_vector_ordering_preserved(monkeypatch):
    """Each input's vector is at the right index after chunking."""

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        # Encode the input index in the first dim for assertion.
        return [[float(int(t.split("-")[1])), 0.0, 0.0] for t in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    inputs = [f"t-{i}" for i in range(150)]
    out = await emb.embed_texts(inputs, settings=_make_settings())

    for i, vec in enumerate(out):
        assert vec[0] == float(i), f"vector at index {i} out of order: {vec}"


@pytest.mark.asyncio
async def test_retry_on_429_then_success(monkeypatch):
    """429 once → backoff → succeed; vectors returned from the second call."""
    calls = {"n": 0}

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            req = httpx.Request("POST", "https://example.invalid/embeddings")
            resp = httpx.Response(429, request=req)
            raise httpx.HTTPStatusError("rate limited", request=req, response=resp)
        return [_vec() for _ in chunk]

    # Skip real backoff sleeps so the test runs fast.
    async def no_sleep(_seconds):  # noqa: ANN001
        return None

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    monkeypatch.setattr(emb.asyncio, "sleep", no_sleep)

    out = await emb.embed_texts(["x", "y"], settings=_make_settings())

    assert calls["n"] == 2
    assert len(out) == 2


@pytest.mark.asyncio
async def test_retry_budget_exhausted_raises(monkeypatch):
    """Four consecutive 503s → raise after the third retry attempt."""
    calls = {"n": 0}

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        calls["n"] += 1
        req = httpx.Request("POST", "https://example.invalid/embeddings")
        resp = httpx.Response(503, request=req)
        raise httpx.HTTPStatusError("bad gateway", request=req, response=resp)

    async def no_sleep(_seconds):  # noqa: ANN001
        return None

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    monkeypatch.setattr(emb.asyncio, "sleep", no_sleep)

    with pytest.raises(httpx.HTTPStatusError):
        await emb.embed_texts(["a"], settings=_make_settings())

    # 1 initial + 3 retries = 4 total
    assert calls["n"] == 4


@pytest.mark.asyncio
async def test_response_length_mismatch_raises(monkeypatch):
    """Provider returns fewer vectors than inputs → raise rather than truncate."""

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        return [_vec()]  # always 1 vector regardless of chunk size

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)

    with pytest.raises(emb.EmbeddingResponseError):
        await emb.embed_texts(["a", "b", "c"], settings=_make_settings())


@pytest.mark.asyncio
async def test_unknown_provider_raises():
    """Provider prefix not in ``SUPPORTED_PROVIDERS`` → typed error."""
    settings = _make_settings(embedding_provider="fictional")
    with pytest.raises(emb.EmbeddingProviderError) as excinfo:
        await emb.embed_texts(["x"], settings=settings)
    msg = str(excinfo.value)
    assert "fictional" in msg
    assert "jina_ai" in msg  # supported list surfaced


@pytest.mark.asyncio
async def test_empty_input_returns_empty_without_calling_provider(monkeypatch):
    fake = AsyncMock(side_effect=AssertionError("provider should not be called"))
    monkeypatch.setattr(emb, "_aembedding_call", fake)

    out = await emb.embed_texts([], settings=_make_settings())
    assert out == []


@pytest.mark.asyncio
async def test_task_kwarg_passed_for_jina(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        captured.update(extra_kwargs)
        captured["model"] = model
        return [_vec() for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    await emb.embed_texts(["x"], settings=_make_settings(), task="text-matching")

    assert captured["model"] == "jina_ai/jina-embeddings-v4"
    assert captured.get("task") == "text-matching"


@pytest.mark.asyncio
async def test_task_kwarg_dropped_for_openai(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        captured.update(extra_kwargs)
        captured["model"] = model
        return [_vec() for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    settings = _make_settings(
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
    )
    await emb.embed_texts(["x"], settings=settings, task="text-matching")

    assert captured["model"] == "openai/text-embedding-3-small"
    assert "task" not in captured


def test_jina_key_bridge_does_not_overwrite_existing_target(monkeypatch):
    """When JINA_AI_API_KEY is already set, the bridge must not overwrite it."""
    monkeypatch.setenv("JINA_AI_API_KEY", "operator-supplied-value")
    settings = _make_settings()
    # ``jina_api_key`` is a separate field from ``embedding_api_key`` — set it
    # explicitly so the bridge has something to copy.
    settings_with_jina = settings.model_copy(update={"jina_api_key": "legacy-value"})

    emb.initialize_embedding_runtime(settings_with_jina)

    assert os.environ["JINA_AI_API_KEY"] == "operator-supplied-value"


def test_jina_key_bridge_seeds_target_when_unset(monkeypatch):
    monkeypatch.delenv("JINA_AI_API_KEY", raising=False)
    settings = _make_settings()
    settings_with_jina = settings.model_copy(update={"jina_api_key": "legacy-key-123"})

    emb.initialize_embedding_runtime(settings_with_jina)

    assert os.environ["JINA_AI_API_KEY"] == "legacy-key-123"


# ─── PR-ζ: embedding dispatch routing for OpenAI-compat shims ─────────────


def test_route_gemini_openai_compat_shim_uses_openai_provider() -> None:
    """Gemini ``/v1beta/openai/`` shim accepts ``POST /v1beta/openai/embeddings``
    in OpenAI shape. LiteLLM's native ``gemini`` embedding handler 404s
    against the shim URL — route via ``openai`` instead so LiteLLM hits the
    right endpoint."""
    provider, model = emb._route_embedding_for_dispatch(
        "gemini",
        "gemini/text-embedding-004",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    assert provider == "openai"
    assert model == "text-embedding-004"


def test_route_gemini_native_keeps_native_provider() -> None:
    """A Gemini endpoint pointed at the native API (no ``/openai/`` in the
    URL) stays on LiteLLM's native ``gemini`` handler. We only re-route
    when the OpenAI-compat shim path is recognisable."""
    provider, model = emb._route_embedding_for_dispatch(
        "gemini",
        "gemini/text-embedding-004",
        "https://generativelanguage.googleapis.com",
    )
    assert provider == "gemini"
    assert model == "gemini/text-embedding-004"


def test_route_jina_v1_shim_uses_openai_provider() -> None:
    """Jina's ``/v1/embeddings`` shim is OpenAI-shaped — route via openai
    SDK to sidestep native-handler quirks."""
    provider, model = emb._route_embedding_for_dispatch(
        "jina_ai",
        "jina_ai/jina-embeddings-v4",
        "https://api.jina.ai/v1",
    )
    assert provider == "openai"
    assert model == "jina-embeddings-v4"


def test_route_jina_non_shim_keeps_native_provider() -> None:
    """An operator-supplied non-``/v1`` Jina URL (e.g. private gateway)
    stays on native handler — they explicitly opted out of the shim."""
    provider, model = emb._route_embedding_for_dispatch(
        "jina_ai",
        "jina_ai/jina-embeddings-v4",
        "https://gateway.internal/jina",
    )
    assert provider == "jina_ai"
    assert model == "jina_ai/jina-embeddings-v4"


def test_route_ollama_v1_shim_uses_openai_provider() -> None:
    """Ollama's OpenAI-compat shim accepts ``/v1/embeddings`` — route via
    openai SDK, same as the chat side."""
    provider, model = emb._route_embedding_for_dispatch(
        "ollama",
        "ollama/nomic-embed-text",
        "http://localhost:11434/v1",
    )
    assert provider == "openai"
    assert model == "nomic-embed-text"


def test_route_openai_passes_through() -> None:
    """OpenAI itself stays openai — no rewrite needed."""
    provider, model = emb._route_embedding_for_dispatch(
        "openai",
        "openai/text-embedding-3-small",
        "https://api.openai.com/v1",
    )
    assert provider == "openai"
    assert model == "openai/text-embedding-3-small"


@pytest.mark.asyncio
async def test_dimensions_kwarg_forwarded(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        captured.update(extra_kwargs)
        return [_vec() for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    settings = _make_settings(embedding_dimensions=1024)
    await emb.embed_texts(["x"], settings=settings)

    assert captured["dimensions"] == 1024


@pytest.mark.asyncio
async def test_api_base_forwarded_when_set(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        captured.update(extra_kwargs)
        return [_vec() for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    settings = _make_settings(embedding_api_base="https://example.invalid/v1")
    await emb.embed_texts(["x"], settings=settings)

    assert captured["api_base"] == "https://example.invalid/v1"


@pytest.mark.asyncio
async def test_api_base_omitted_when_blank(monkeypatch):
    captured: dict[str, Any] = {}

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        captured.update(extra_kwargs)
        return [_vec() for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)
    settings = _make_settings(embedding_api_base="")
    await emb.embed_texts(["x"], settings=settings)

    assert "api_base" not in captured
