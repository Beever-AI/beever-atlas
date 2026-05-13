"""PR15: provider-matrix regression suite for ``llm_dispatch``.

Background — two production bugs slipped through PR14:

  1. **Google AI (Gemini) via ``/openai/`` shim**: ``route_for_endpoint``
     returned ``("openai", "gemini-2.5-flash", False)`` and dispatch called
     ``litellm.acompletion(model="gemini-2.5-flash", api_base="...openai/...", ...)``
     without ``custom_llm_provider``. LiteLLM matched ``gemini-2.5-flash`` to
     its native gemini model registry, ignored ``api_base``, and surfaced an
     error whose body echoed the ``AIzaSy`` key prefix → the credential
     redactor masked the whole message → operator saw
     ``"(redacted — upstream text may contain credential fragments)"``.

  2. **Ollama via ``/v1`` shim**: dispatch called
     ``litellm.acompletion(model="gemma4:e2b", api_base="http://localhost:11434/v1")``.
     ``gemma4:e2b`` matches nothing in LiteLLM's registry, so it raised
     ``BadRequestError: LLM Provider NOT provided``.

The fix in ``llm_dispatch.py`` makes ``custom_llm_provider`` the authoritative
routing signal for **every** dispatch (completion + embedding). The matrix
below pins down the expected ``(model, custom_llm_provider, api_base, api_key)``
kwargs LiteLLM receives for every preset in the Endpoint catalog, so a future
refactor of ``route_for_endpoint`` or the dispatch wrapper that re-introduces
either mis-routing failure will break this suite first.

Coverage approach:
  * ``test_route_for_endpoint_matrix`` — pure-function parametrized test over
    ``route_for_endpoint``. Cheap, fast, fails on regressions in the routing
    table itself.
  * ``test_dispatch_completion_matrix`` — patches ``litellm.acompletion`` and
    invokes ``dispatch_completion`` directly with the ``(provider, model)``
    each row would yield. Asserts the wire-form kwargs.
  * ``test_dispatch_embedding_matrix`` — same shape for the embedding-only
    presets via ``dispatch_embedding`` + ``litellm.aembedding``.
  * ``test_dispatch_assignment_resolved`` — end-to-end via
    ``ResolvedAssignment`` + ``dispatch_assignment`` to exercise the
    real call path the agents take (not just the Test Connection probe).

Bedrock / Vertex are exercised at the routing level but ``xfail``-ed at the
dispatch level because their credential wiring is special-cased and not in
scope for the kwarg-shape regression this suite locks in.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from beever_atlas.llm.assignments import ResolvedAssignment
from beever_atlas.services.llm_dispatch import (
    _split_model_for_litellm,
    dispatch_assignment,
    dispatch_completion,
    dispatch_embedding,
    route_for_endpoint,
)


# ────────────────────────────────────────────────────────────────────────
# Throttle bypass — every test in this module mocks LiteLLM so the real
# bucket isn't useful and adds latency. ``llm_throttle.get_llm_throttle``
# returns a process-wide singleton; replace its ``acquire`` with an
# async no-op context manager via autouse fixture.
# ────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _bypass_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the throttle bucket — these tests assert kwargs, not pacing."""
    from contextlib import asynccontextmanager

    from beever_atlas.services import llm_throttle

    @asynccontextmanager
    async def _noop(*_args: Any, **_kwargs: Any):
        yield

    class _FakeThrottle:
        def acquire(self, *_args: Any, **_kwargs: Any):
            return _noop()

        def report_429(self, *_args: Any, **_kwargs: Any) -> None:
            return None

    monkeypatch.setattr(llm_throttle, "get_llm_throttle", lambda: _FakeThrottle())


def _fake_response() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    return r


def _fake_embedding_response() -> dict[str, Any]:
    return {"data": [{"embedding": [0.0] * 4, "index": 0}], "model": "x"}


# ────────────────────────────────────────────────────────────────────────
# Section 1 — pure routing matrix. One row per preset; asserts what
# ``route_for_endpoint`` returns end-to-end.
# ────────────────────────────────────────────────────────────────────────


# (preset, base_url, model, expected_provider, expected_route_model, drop_base_url)
ROUTING_MATRIX: list[tuple[str, str, str, str, str, bool]] = [
    # Google AI /openai/ shim — bare model + openai provider; api_base honoured.
    (
        "google_ai",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
        "models/gemini-2.5-flash",
        "openai",
        "gemini-2.5-flash",
        False,
    ),
    # Google AI native — gemini provider, drop_base_url.
    (
        "google_ai",
        "",
        "gemini-2.5-flash",
        "gemini",
        "gemini/gemini-2.5-flash",
        True,
    ),
    # OpenAI proper.
    (
        "openai",
        "https://api.openai.com/v1",
        "gpt-4o-mini",
        "openai",
        "openai/gpt-4o-mini",
        False,
    ),
    # Anthropic.
    (
        "anthropic",
        "https://api.anthropic.com/v1",
        "claude-haiku-4-5",
        "anthropic",
        "anthropic/claude-haiku-4-5",
        False,
    ),
    # Mistral.
    (
        "mistral",
        "https://api.mistral.ai/v1",
        "mistral-small-latest",
        "mistral",
        "mistral/mistral-small-latest",
        False,
    ),
    # DeepSeek.
    (
        "deepseek",
        "https://api.deepseek.com",
        "deepseek-chat",
        "deepseek",
        "deepseek/deepseek-chat",
        False,
    ),
    # Groq (note: groq's base_url already has ``/openai/v1`` but the preset
    # is mapped to its native LiteLLM ``groq`` provider, not openai).
    (
        "groq",
        "https://api.groq.com/openai/v1",
        "llama-3.3-70b-versatile",
        "groq",
        "groq/llama-3.3-70b-versatile",
        False,
    ),
    # xAI.
    (
        "xai",
        "https://api.x.ai/v1",
        "grok-4",
        "xai",
        "xai/grok-4",
        False,
    ),
    # MiniMax.
    (
        "minimax",
        "https://api.minimaxi.com/v1",
        "abab6.5s-chat",
        "minimax",
        "minimax/abab6.5s-chat",
        False,
    ),
    # together_ai.
    (
        "together_ai",
        "https://api.together.xyz/v1",
        "llama-3.3-70b",
        "together_ai",
        "together_ai/llama-3.3-70b",
        False,
    ),
    # Ollama via /v1 OpenAI-compat shim — openai provider + bare model.
    (
        "ollama",
        "http://localhost:11434/v1",
        "gemma4:e2b",
        "openai",
        "gemma4:e2b",
        False,
    ),
    # Ollama native — ollama_chat provider.
    (
        "ollama",
        "http://localhost:11434",
        "llama3.2",
        "ollama_chat",
        "ollama_chat/llama3.2",
        False,
    ),
    # vLLM — every OpenAI-compat preset routes through openai provider.
    # HF-org slashes (``meta-llama/...``) must NOT be treated as LiteLLM
    # provider prefixes; preset-driven routing wins.
    (
        "vllm",
        "http://localhost:8000/v1",
        "meta-llama/Llama-3.3-70B",
        "openai",
        "meta-llama/Llama-3.3-70B",
        False,
    ),
    # LM Studio.
    (
        "lmstudio",
        "http://localhost:1234/v1",
        "phi-4",
        "openai",
        "phi-4",
        False,
    ),
    # OpenRouter.
    (
        "openrouter",
        "https://openrouter.ai/api/v1",
        "anthropic/claude-3-opus",
        "openai",
        "anthropic/claude-3-opus",
        False,
    ),
    # LiteLLM proxy.
    (
        "litellm_proxy",
        "http://localhost:4000",
        "my-router-tag",
        "openai",
        "my-router-tag",
        False,
    ),
    # Custom OpenAI-compatible.
    (
        "custom",
        "https://my.proxy/v1",
        "whatever-model",
        "openai",
        "whatever-model",
        False,
    ),
    # Bedrock — preset_to_provider falls through to the preset key itself.
    (
        "bedrock",
        "",
        "amazon.titan-text-express-v1",
        "bedrock",
        "bedrock/amazon.titan-text-express-v1",
        False,
    ),
    # Vertex AI.
    (
        "vertex_ai",
        "",
        "gemini-2.5-pro",
        "vertex_ai",
        "vertex_ai/gemini-2.5-pro",
        False,
    ),
]


@pytest.mark.parametrize("preset,base_url,model,exp_provider,exp_model,exp_drop", ROUTING_MATRIX)
def test_route_for_endpoint_matrix(
    preset: str,
    base_url: str,
    model: str,
    exp_provider: str,
    exp_model: str,
    exp_drop: bool,
) -> None:
    """Pure-function pin for ``route_for_endpoint``. One row per preset
    — guards against silent regressions in the routing table."""
    provider, routed_model, drop_base_url = route_for_endpoint(preset, base_url, model)
    assert provider == exp_provider, f"{preset}: provider mismatch"
    assert routed_model == exp_model, f"{preset}: model mismatch"
    assert drop_base_url is exp_drop, f"{preset}: drop_base_url mismatch"


def test_route_for_endpoint_raises_on_embedding_only_presets() -> None:
    """Embedding-only presets cannot be routed through chat dispatch."""
    for preset in ("jina_ai", "voyage", "cohere"):
        with pytest.raises(ValueError, match="embedding-only"):
            route_for_endpoint(preset, "https://example.com/v1", "model-x")


# ────────────────────────────────────────────────────────────────────────
# Section 2 — pin _split_model_for_litellm directly so the prefix-stripping
# behaviour is locked in independent of the dispatch wrappers.
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "provider,model,exp_model,exp_provider",
    [
        # Matching prefix → strip, keep provider.
        ("openai", "openai/gpt-4o-mini", "gpt-4o-mini", "openai"),
        ("anthropic", "anthropic/claude-haiku-4-5", "claude-haiku-4-5", "anthropic"),
        ("gemini", "gemini/gemini-2.5-flash", "gemini-2.5-flash", "gemini"),
        ("ollama_chat", "ollama_chat/llama3.2", "llama3.2", "ollama_chat"),
        # No prefix → pass through, provider unchanged.
        ("openai", "gpt-4o-mini", "gpt-4o-mini", "openai"),
        ("openai", "gemma4:e2b", "gemma4:e2b", "openai"),
        # Non-matching slash (any flavour) → routed provider wins; the model
        # is forwarded unchanged because the slash is part of its id. Covers
        # OpenRouter's vendor prefix (``anthropic/claude-3-opus``) and HF-org
        # style ids on vLLM (``meta-llama/Llama-3.3-70B``).
        ("openai", "anthropic/claude-haiku-4-5", "anthropic/claude-haiku-4-5", "openai"),
        ("openai", "meta-llama/Llama-3.3-70B", "meta-llama/Llama-3.3-70B", "openai"),
    ],
)
def test_split_model_for_litellm_matrix(
    provider: str, model: str, exp_model: str, exp_provider: str
) -> None:
    out_model, out_provider = _split_model_for_litellm(provider, model)
    assert out_model == exp_model
    assert out_provider == exp_provider


# ────────────────────────────────────────────────────────────────────────
# Section 3 — completion dispatch matrix. For each routable preset, call
# ``dispatch_completion`` with the values ``route_for_endpoint`` would
# yield and assert the LiteLLM kwargs end up canonical.
# ────────────────────────────────────────────────────────────────────────


# Subset of ROUTING_MATRIX excluding the special-cred presets (bedrock /
# vertex_ai). Their dispatch wiring needs ``aws_access_key_id`` /
# ``vertex_credentials`` plumbed through ``dispatch_assignment``; the bare
# ``dispatch_completion`` row here would fall through with no creds and is
# not a useful regression pin.
_COMPLETION_ROWS: list[tuple[str, str, str, str, str, bool]] = [
    r for r in ROUTING_MATRIX if r[0] not in ("bedrock", "vertex_ai")
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "preset,base_url,model,exp_provider,exp_route_model,exp_drop", _COMPLETION_ROWS
)
async def test_dispatch_completion_matrix(
    preset: str,
    base_url: str,
    model: str,
    exp_provider: str,
    exp_route_model: str,
    exp_drop: bool,
) -> None:
    """End-to-end: route, then dispatch. Asserts LiteLLM gets the right
    ``(model, custom_llm_provider, api_base, api_key)`` kwargs.

    The first part validates the routing matrix; the second part validates
    that dispatch correctly strips the ``<provider>/`` prefix when it
    matches the routed provider so the bare id + explicit ``custom_llm_provider``
    reach LiteLLM."""
    provider, route_model, drop_base_url = route_for_endpoint(preset, base_url, model)
    assert provider == exp_provider
    assert route_model == exp_route_model
    assert drop_base_url is exp_drop

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_response()

    extra_kwargs: dict[str, Any] = {"api_key": "test-key"}
    if not drop_base_url and base_url:
        extra_kwargs["api_base"] = base_url

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_completion(
            provider=provider,
            model=route_model,
            messages=[{"role": "user", "content": "hi"}],
            **extra_kwargs,
        )

    # custom_llm_provider is the load-bearing fix — it MUST be present and
    # equal to ``provider`` (after prefix-strip reconciliation).
    assert captured["custom_llm_provider"] == provider, (
        f"{preset}: expected custom_llm_provider={provider}, "
        f"got {captured.get('custom_llm_provider')}"
    )

    # Model is forwarded after stripping a matching prefix. Compute the
    # expected wire-form model the same way ``_split_model_for_litellm`` does.
    expected_wire_model, _ = _split_model_for_litellm(provider, route_model)
    assert captured["model"] == expected_wire_model

    # api_base behaviour matches drop_base_url semantics.
    if drop_base_url or not base_url:
        assert "api_base" not in captured, f"{preset}: api_base should be dropped"
    else:
        assert captured["api_base"] == base_url

    assert captured["api_key"] == "test-key"
    assert captured["messages"] == [{"role": "user", "content": "hi"}]


# ────────────────────────────────────────────────────────────────────────
# Section 4 — embedding dispatch matrix. ``dispatch_embedding`` carries
# the same ``custom_llm_provider`` semantics as completion. Embedding-only
# presets are routed via ``preset_to_provider`` + raw ``provider/model``
# (the Test Connection probe path), so the wire form differs slightly.
# ────────────────────────────────────────────────────────────────────────


# (preset, base_url, model, expected_provider_passed, expected_wire_model)
EMBEDDING_MATRIX: list[tuple[str, str, str, str, str]] = [
    (
        "jina_ai",
        "https://api.jina.ai/v1",
        "jina-embeddings-v4",
        "jina_ai",
        "jina-embeddings-v4",
    ),
    (
        "voyage",
        "https://api.voyageai.com/v1",
        "voyage-3-large",
        "voyage",
        "voyage-3-large",
    ),
    (
        "cohere",
        "https://api.cohere.com/v1",
        "embed-multilingual-v3.0",
        "cohere",
        "embed-multilingual-v3.0",
    ),
    # OpenAI text-embedding-3 family — flows through the same dispatch but
    # the caller (the embedding runtime) constructs ``openai/<model>``.
    (
        "openai",
        "https://api.openai.com/v1",
        "openai/text-embedding-3-large",
        "openai",
        "text-embedding-3-large",
    ),
    # Native Gemini embedding model.
    (
        "gemini",
        "",
        "gemini/gemini-embedding-001",
        "gemini",
        "gemini-embedding-001",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("preset,base_url,model,exp_provider,exp_wire_model", EMBEDDING_MATRIX)
async def test_dispatch_embedding_matrix(
    preset: str,
    base_url: str,
    model: str,
    exp_provider: str,
    exp_wire_model: str,
) -> None:
    """Embedding dispatch must pass ``custom_llm_provider`` so providers like
    Jina (whose model id ``jina-embeddings-v4`` is in LiteLLM's registry but
    is also accepted by other providers' embeddings endpoints) route
    deterministically — and so Ollama-style bare ids don't fail provider
    inference."""
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _fake_embedding_response()

    extra_kwargs: dict[str, Any] = {"api_key": "test-embed-key"}
    if base_url:
        extra_kwargs["api_base"] = base_url

    with patch("litellm.aembedding", side_effect=fake_aembedding):
        await dispatch_embedding(
            provider=exp_provider,
            model=model,
            input=["test"],
            **extra_kwargs,
        )

    assert captured["custom_llm_provider"] == exp_provider
    assert captured["model"] == exp_wire_model
    if base_url:
        assert captured["api_base"] == base_url
    assert captured["api_key"] == "test-embed-key"
    assert captured["input"] == ["test"]


# ────────────────────────────────────────────────────────────────────────
# Section 4.5 — PR-ζ.2: dimensions bypass for non-OpenAI models routed
# through the ``openai`` custom_llm_provider (Gemini ``/v1beta/openai/``
# shim, Jina ``/v1`` shim, Ollama ``/v1`` shim).
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dispatch_embedding_adds_allowed_openai_params_for_shim_model() -> None:
    """When ``custom_llm_provider=openai`` AND model name lacks
    ``text-embedding-3`` AND ``dimensions=`` is set, ``dispatch_embedding``
    MUST inject ``allowed_openai_params=["dimensions"]``. LiteLLM has a
    hardcoded check (utils.py L3306-3315) that raises ``UnsupportedParamsError``
    otherwise — ``drop_params`` does NOT bypass this specific guard."""
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _fake_embedding_response()

    with patch("litellm.aembedding", side_effect=fake_aembedding):
        await dispatch_embedding(
            provider="openai",  # routed for Gemini's /v1beta/openai/ shim
            model="text-embedding-004",
            input=["x"],
            api_base="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key="AIza-test",
            dimensions=768,
        )

    assert captured["custom_llm_provider"] == "openai"
    # The bypass kwarg is what unblocks the boot probe.
    assert captured["allowed_openai_params"] == ["dimensions"]
    assert captured["dimensions"] == 768


@pytest.mark.asyncio
async def test_dispatch_embedding_skips_bypass_for_real_openai_models() -> None:
    """A genuine OpenAI ``text-embedding-3-*`` request must NOT trigger the
    bypass — the kwarg is already supported natively. Adding it would be a
    no-op but the conditional should still skip cleanly."""
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _fake_embedding_response()

    with patch("litellm.aembedding", side_effect=fake_aembedding):
        await dispatch_embedding(
            provider="openai",
            model="openai/text-embedding-3-small",
            input=["x"],
            api_key="sk-test",
            dimensions=512,
        )

    # bypass NOT applied — the model name already matches openai's allow-list.
    assert "allowed_openai_params" not in captured
    assert captured["dimensions"] == 512


@pytest.mark.asyncio
async def test_dispatch_embedding_skips_bypass_when_no_dimensions_kwarg() -> None:
    """Embedding calls without ``dimensions=`` skip the bypass injection."""
    captured: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _fake_embedding_response()

    with patch("litellm.aembedding", side_effect=fake_aembedding):
        await dispatch_embedding(
            provider="openai",
            model="jina-embeddings-v4",
            input=["x"],
            api_base="https://api.jina.ai/v1",
            api_key="jina-test",
        )

    assert "allowed_openai_params" not in captured
    assert "dimensions" not in captured


# ────────────────────────────────────────────────────────────────────────
# Section 5 — dispatch_assignment end-to-end on a handful of representative
# rows. This is the path agents actually take (not the Test Connection
# probe). Exercises the ``ResolvedAssignment`` plumbing as well so a future
# regression in ``LLMProvider.resolve_for_call`` won't sneak past.
# ────────────────────────────────────────────────────────────────────────


def _make_resolved(
    *,
    provider: str,
    litellm_model: str,
    base_url: str | None,
    api_key: str | None = "test-key",
    consumer: str = "qa_agent",
    aws_credentials: dict[str, str] | None = None,
    vertex_credentials: dict[str, str] | None = None,
) -> ResolvedAssignment:
    return ResolvedAssignment(
        consumer=consumer,
        endpoint_id="endpoint-uuid-1",
        provider=provider,
        litellm_model=litellm_model,
        base_url=base_url,
        api_key=api_key,
        aws_credentials=aws_credentials,
        vertex_credentials=vertex_credentials,
        extra_headers={},
        temperature=None,
        max_tokens=None,
        response_format=None,
        dimensions=None,
        task=None,
    )


# (label, ResolvedAssignment kwargs, expected wire-form (model, provider, api_base))
ASSIGNMENT_ROWS: list[tuple[str, dict[str, Any], str, str, str | None]] = [
    (
        "google_ai_openai_shim",
        dict(
            provider="openai",
            litellm_model="gemini-2.5-flash",
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            api_key="AIzaSy-real-key-redact-me",
        ),
        "gemini-2.5-flash",
        "openai",
        "https://generativelanguage.googleapis.com/v1beta/openai/",
    ),
    (
        "ollama_v1_shim",
        dict(
            provider="openai",
            litellm_model="gemma4:e2b",
            base_url="http://localhost:11434/v1",
            api_key=None,
        ),
        "gemma4:e2b",
        "openai",
        "http://localhost:11434/v1",
    ),
    (
        "anthropic_native",
        dict(
            provider="anthropic",
            litellm_model="anthropic/claude-haiku-4-5",
            base_url="https://api.anthropic.com/v1",
            api_key="sk-ant-test-XYZ-AAAA",
        ),
        "claude-haiku-4-5",
        "anthropic",
        "https://api.anthropic.com/v1",
    ),
    (
        "gemini_native",
        dict(
            provider="gemini",
            litellm_model="gemini/gemini-2.5-flash",
            base_url=None,  # drop_base_url path
            api_key="AIzaSy-native-key",
        ),
        "gemini-2.5-flash",
        "gemini",
        None,
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,resolved_kwargs,exp_model,exp_provider,exp_api_base",
    ASSIGNMENT_ROWS,
    ids=[row[0] for row in ASSIGNMENT_ROWS],
)
async def test_dispatch_assignment_matrix(
    label: str,
    resolved_kwargs: dict[str, Any],
    exp_model: str,
    exp_provider: str,
    exp_api_base: str | None,
) -> None:
    """The real-dispatch path (not Test Connection): build a ResolvedAssignment
    and confirm the kwargs that reach LiteLLM match the canonical wire form."""
    resolved = _make_resolved(**resolved_kwargs)

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_assignment(
            assignment=resolved,
            messages=[{"role": "user", "content": "ping"}],
        )

    assert captured["model"] == exp_model
    assert captured["custom_llm_provider"] == exp_provider
    if exp_api_base is None:
        assert "api_base" not in captured
    else:
        assert captured["api_base"] == exp_api_base


# ────────────────────────────────────────────────────────────────────────
# Section 6 — Bedrock / Vertex dispatch shape. Routing returns them through
# the default ``preset_to_provider`` path (``bedrock`` / ``vertex_ai`` as
# the LiteLLM provider). Dispatch_assignment plumbs the credentials through
# ``aws_*`` / ``vertex_credentials`` kwargs. The shape pin below catches
# regressions in either layer. Note these tests don't talk to real AWS/GCP
# (LiteLLM is fully mocked) so no real boto / google-auth fixture is needed.
# ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "preset,model,exp_provider",
    [
        ("bedrock", "amazon.titan-text-express-v1", "bedrock"),
        ("vertex_ai", "gemini-2.5-pro", "vertex_ai"),
    ],
)
def test_bedrock_vertex_routing(preset: str, model: str, exp_provider: str) -> None:
    """Bedrock + Vertex route through the native LiteLLM provider."""
    provider, _model, _drop = route_for_endpoint(preset, "", model)
    assert provider == exp_provider


@pytest.mark.asyncio
async def test_dispatch_assignment_bedrock_shape() -> None:
    """Bedrock dispatch forwards AWS creds via the LiteLLM ``aws_*`` kwargs
    and ``custom_llm_provider=bedrock`` so the boto path is selected."""
    resolved = _make_resolved(
        provider="bedrock",
        litellm_model="bedrock/amazon.titan-text-express-v1",
        base_url=None,
        api_key=None,
        aws_credentials={
            "access_key_id": "AKIA-test",
            "secret_access_key": "secret-test",
            "region": "us-east-1",
        },
    )

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_assignment(
            assignment=resolved, messages=[{"role": "user", "content": "ping"}]
        )

    assert captured["custom_llm_provider"] == "bedrock"
    assert captured["model"] == "amazon.titan-text-express-v1"
    assert captured["aws_access_key_id"] == "AKIA-test"
    assert captured["aws_secret_access_key"] == "secret-test"
    assert captured["aws_region_name"] == "us-east-1"


@pytest.mark.asyncio
async def test_dispatch_assignment_vertex_shape() -> None:
    """Vertex dispatch forwards the SA JSON via ``vertex_credentials`` and
    ``custom_llm_provider=vertex_ai`` so the GCP auth path is selected."""
    resolved = _make_resolved(
        provider="vertex_ai",
        litellm_model="vertex_ai/gemini-2.5-pro",
        base_url=None,
        api_key=None,
        vertex_credentials={"sa_json": '{"type":"service_account"}'},
    )

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_assignment(
            assignment=resolved, messages=[{"role": "user", "content": "ping"}]
        )

    assert captured["custom_llm_provider"] == "vertex_ai"
    assert captured["model"] == "gemini-2.5-pro"
    assert captured["vertex_credentials"] == '{"type":"service_account"}'
