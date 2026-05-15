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


# ─── PR-ζ.3: embedding dispatch routing — Gemini native + shim OpenAI ───


def test_route_gemini_always_native_drops_api_base_shim_url() -> None:
    """PR-ζ.3: Gemini ``/v1beta/openai/`` shim 404s on embeddings (the shim
    internally proxies to ``v1main`` where ``text-embedding-*`` models don't
    exist). Route through LiteLLM's NATIVE gemini handler and DROP api_base
    so LiteLLM uses Google's default native URL —
    ``/v1beta/models/<model>:batchEmbedContents`` — which works."""
    provider, model, drop_api_base = emb._route_embedding_for_dispatch(
        "gemini",
        "gemini/text-embedding-004",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    )
    assert provider == "gemini"
    assert model == "gemini/text-embedding-004"
    assert drop_api_base is True


def test_route_gemini_always_native_drops_api_base_native_url_too() -> None:
    """Even when api_base looks native, drop it — LiteLLM's gemini handler
    builds its own URL; a custom api_base would confuse the URL builder."""
    provider, model, drop_api_base = emb._route_embedding_for_dispatch(
        "gemini",
        "gemini/text-embedding-004",
        "https://generativelanguage.googleapis.com",
    )
    assert provider == "gemini"
    assert model == "gemini/text-embedding-004"
    assert drop_api_base is True


def test_route_jina_v1_shim_uses_openai_provider() -> None:
    """Jina's ``/v1/embeddings`` shim is genuinely OpenAI-shaped — route via
    openai SDK, keep api_base."""
    provider, model, drop_api_base = emb._route_embedding_for_dispatch(
        "jina_ai",
        "jina_ai/jina-embeddings-v4",
        "https://api.jina.ai/v1",
    )
    assert provider == "openai"
    assert model == "jina-embeddings-v4"
    assert drop_api_base is False


def test_route_jina_non_shim_keeps_native_provider() -> None:
    """An operator-supplied non-``/v1`` Jina URL (e.g. private gateway)
    stays on native handler — they explicitly opted out of the shim."""
    provider, model, drop_api_base = emb._route_embedding_for_dispatch(
        "jina_ai",
        "jina_ai/jina-embeddings-v4",
        "https://gateway.internal/jina",
    )
    assert provider == "jina_ai"
    assert model == "jina_ai/jina-embeddings-v4"
    assert drop_api_base is False


def test_route_ollama_v1_shim_uses_openai_provider() -> None:
    """Ollama's OpenAI-compat shim accepts ``/v1/embeddings`` — route via
    openai SDK, same as the chat side."""
    provider, model, drop_api_base = emb._route_embedding_for_dispatch(
        "ollama",
        "ollama/nomic-embed-text",
        "http://localhost:11434/v1",
    )
    assert provider == "openai"
    assert model == "nomic-embed-text"
    assert drop_api_base is False


def test_route_openai_v1_uses_bare_model() -> None:
    """OpenAI's canonical ``https://api.openai.com/v1`` is the OpenAI-compat
    shim path by definition. Rule 2 strips the ``openai/`` prefix so LiteLLM
    sends the bare model id to the openai SDK — the standard form."""
    provider, model, drop_api_base = emb._route_embedding_for_dispatch(
        "openai",
        "openai/text-embedding-3-small",
        "https://api.openai.com/v1",
    )
    assert provider == "openai"
    assert model == "text-embedding-3-small"
    assert drop_api_base is False


# ─── Generalised routing matrix — covers every provider switch ──────────


@pytest.mark.parametrize(
    "provider,model,api_base,exp_provider,exp_model,exp_drop",
    [
        # Cohere — native handler regardless of url (/v1 is Cohere's OWN shape,
        # not OpenAI-compat — /embed not /embeddings).
        (
            "cohere",
            "cohere/embed-english-v3.0",
            "https://api.cohere.ai/v1",
            "cohere",
            "cohere/embed-english-v3.0",
            False,
        ),
        # Voyage — native, no shim.
        (
            "voyage",
            "voyage/voyage-3",
            "https://api.voyageai.com/v1",
            "voyage",
            "voyage/voyage-3",
            False,
        ),
        # Mistral — native, no shim.
        (
            "mistral",
            "mistral/mistral-embed",
            "https://api.mistral.ai/v1",
            "mistral",
            "mistral/mistral-embed",
            False,
        ),
        # Bedrock — native SDK path.
        (
            "bedrock",
            "bedrock/amazon.titan-embed-text-v2:0",
            None,
            "bedrock",
            "bedrock/amazon.titan-embed-text-v2:0",
            False,
        ),
        # vLLM proxy — preset maps to ``openai``, /v1 → rule 2 (bare).
        (
            "openai",
            "openai/llama-3-embed",
            "https://vllm.internal/v1",
            "openai",
            "llama-3-embed",
            False,
        ),
        # LiteLLM Proxy — preset maps to ``openai``, /v1 → rule 2.
        (
            "openai",
            "openai/proxy-model",
            "https://litellm-proxy.internal/v1",
            "openai",
            "proxy-model",
            False,
        ),
        # Empty / missing api_base — native handler with no override.
        ("mistral", "mistral/mistral-embed", None, "mistral", "mistral/mistral-embed", False),
        ("mistral", "mistral/mistral-embed", "", "mistral", "mistral/mistral-embed", False),
    ],
)
def test_route_embedding_matrix(
    provider: str,
    model: str,
    api_base: str | None,
    exp_provider: str,
    exp_model: str,
    exp_drop: bool,
) -> None:
    """PR-ζ.4: comprehensive coverage of the routing decision tree across
    every provider switch operators can realistically configure."""
    got_provider, got_model, got_drop = emb._route_embedding_for_dispatch(provider, model, api_base)
    assert got_provider == exp_provider
    assert got_model == exp_model
    assert got_drop is exp_drop


@pytest.mark.asyncio
async def test_aembedding_call_strips_api_base_for_gemini(monkeypatch):
    """PR-ζ.3: when routing Gemini embedding to the native handler,
    ``_aembedding_call`` MUST strip ``api_base`` from the dispatch kwargs.
    LiteLLM's native gemini embedding handler builds its own URL — passing
    a stale shim ``api_base`` would 404."""
    captured_dispatch: dict[str, Any] = {}

    async def fake_dispatch(**kwargs):
        captured_dispatch.update(kwargs)

        class _R:
            def model_dump(self):
                return {"data": [{"embedding": [0.1] * 8}]}

        return _R()

    monkeypatch.setattr("beever_atlas.services.llm_dispatch.dispatch_embedding", fake_dispatch)

    await emb._aembedding_call(
        model="gemini/text-embedding-004",
        chunk=["hello"],
        extra_kwargs={
            "api_base": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "api_key": "AIza-test",
            "dimensions": 768,
        },
        provider="gemini",
    )

    # Routed to native gemini handler.
    assert captured_dispatch["provider"] == "gemini"
    assert captured_dispatch["model"] == "gemini/text-embedding-004"
    # The shim api_base was stripped — LiteLLM uses Google's default native URL.
    assert "api_base" not in captured_dispatch
    # Other kwargs survive the strip.
    assert captured_dispatch["api_key"] == "AIza-test"
    assert captured_dispatch["dimensions"] == 768


@pytest.mark.asyncio
async def test_aembedding_call_keeps_api_base_for_jina_v1_shim(monkeypatch):
    """For Jina ``/v1`` shim routing (openai provider), api_base is REQUIRED —
    LiteLLM's openai SDK posts to ``<api_base>/embeddings``."""
    captured_dispatch: dict[str, Any] = {}

    async def fake_dispatch(**kwargs):
        captured_dispatch.update(kwargs)

        class _R:
            def model_dump(self):
                return {"data": [{"embedding": [0.1] * 8}]}

        return _R()

    monkeypatch.setattr("beever_atlas.services.llm_dispatch.dispatch_embedding", fake_dispatch)

    await emb._aembedding_call(
        model="jina_ai/jina-embeddings-v4",
        chunk=["hello"],
        extra_kwargs={
            "api_base": "https://api.jina.ai/v1",
            "api_key": "jina-test",
        },
        provider="jina_ai",
    )

    assert captured_dispatch["provider"] == "openai"
    assert captured_dispatch["model"] == "jina-embeddings-v4"
    # api_base survives — needed for the openai SDK to find the shim.
    assert captured_dispatch["api_base"] == "https://api.jina.ai/v1"


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
