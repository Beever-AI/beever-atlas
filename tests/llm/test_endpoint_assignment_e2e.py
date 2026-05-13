"""End-to-end simulation of the Endpoint + Assignment dispatch path.

Proves the whole design works end-to-end without hitting the network:

1. Operator creates an Endpoint (encrypted credential persisted)
2. Boot hydration loads the credential into the runtime cache
3. Operator creates an Assignment with per-call params
4. ``LLMProvider.resolve_for_call(consumer)`` returns a ``ResolvedAssignment``
5. ``dispatch_assignment`` calls ``litellm.acompletion`` with the right kwargs
6. The per-Endpoint throttle bucket gates the call
7. Failover: circuit breaker open → fallback Endpoint used
8. Capability validation: incompatible model raises 422 at PUT time

This is the test the proposal's design D14 implies: a single suite that
exercises every layer together (storage + crypto + runtime cache + resolver
+ dispatch + throttle + failover + validation) to catch design-time gaps.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from beever_atlas.llm.agent_credentials import (
    clear_all_runtime_credentials,
    hydrate_runtime_credentials,
)
from beever_atlas.llm.assignments import Assignment, AssignmentStore
from beever_atlas.llm.endpoints import EndpointStore
from beever_atlas.llm.provider import (
    CircuitBreakerOpenForBothPrimaryAndFallback,
    LLMProvider,
)
from beever_atlas.services.circuit_breaker import reset_circuit_breaker_for_tests
from beever_atlas.services.llm_dispatch import dispatch_assignment
from beever_atlas.services.llm_throttle import reset_llm_throttle_for_tests


# ─── Fake Mongo collection (same shape as previous test files) ───────────


class _AsyncCursor:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_AsyncCursor":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _Result:
    def __init__(self, matched: int = 0) -> None:
        self.matched_count = matched
        self.modified_count = matched


class _FakeCollection:
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any], _proj: Any = None) -> _AsyncCursor:
        return _AsyncCursor([d for d in self._docs if self._matches(d, query)])

    async def find_one(self, query: dict[str, Any], _proj: Any = None) -> Any:
        for d in self._docs:
            if self._matches(d, query):
                return d
        return None

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self._docs.append(dict(doc))

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _Result:
        for d in self._docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return _Result(matched=1)
        if upsert:
            new = dict(update.get("$set", {}))
            new.update(query)
            self._docs.append(new)
        return _Result(matched=0)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if "$or" in query:
            return any(_FakeCollection._matches(doc, q) for q in query["$or"])
        return all(doc.get(k) == v for k, v in query.items())


def _stores() -> Any:
    return SimpleNamespace(
        mongodb=SimpleNamespace(
            db={"endpoints": _FakeCollection(), "llm_assignments": _FakeCollection()}
        )
    )


def _settings_stub() -> Any:
    return SimpleNamespace(
        llm_fast_model="gemini-2.5-flash",
        llm_quality_model="gemini-2.5-pro",
    )


# ─── Per-test fixtures ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_global_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)
    monkeypatch.setenv("LLM_USE_LITELLM_FOR_GEMINI", "true")
    from beever_atlas.infra.config import get_settings

    get_settings.cache_clear()
    clear_all_runtime_credentials()
    reset_circuit_breaker_for_tests()
    reset_llm_throttle_for_tests()


def _mock_litellm_response(content: str = "ok") -> MagicMock:
    """Build a fake LiteLLM response shaped for ``.choices[0].message.content``."""
    resp = MagicMock()
    resp.status_code = 200
    choice = MagicMock()
    choice.message.content = content
    resp.choices = [choice]
    return resp


# ─── E2E #1: full happy path — operator setup → resolve → dispatch ──────


@pytest.mark.asyncio
async def test_e2e_full_happy_path() -> None:
    """The full flow: create Endpoint → store credential → create Assignment
    → resolve_for_call → dispatch_assignment → LiteLLM called correctly."""
    stores = _stores()

    # 1. Operator creates an Anthropic Endpoint with their key.
    ep_store = EndpointStore(stores.mongodb)
    endpoint = await ep_store.create(
        name="Anthropic prod",
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-ant-real-key-XYZ",
        models=["claude-sonnet-4-6"],
        rpm=100,
    )

    # 2. Boot hydrates the runtime credential cache.
    await hydrate_runtime_credentials(stores)

    # 3. Operator creates an Assignment for qa_agent.
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="qa_agent",
            endpoint_id=endpoint.id,
            model="claude-sonnet-4-6",
            temperature=0.2,
            max_tokens=2048,
        )
    )

    # 4. LLMProvider resolves the assignment for a call.
    provider = LLMProvider(_settings_stub())
    resolved = await provider.resolve_for_call("qa_agent", stores=stores)
    assert resolved is not None
    assert resolved.consumer == "qa_agent"
    assert resolved.endpoint_id == endpoint.id
    assert resolved.provider == "anthropic"
    assert resolved.litellm_model == "anthropic/claude-sonnet-4-6"
    assert resolved.base_url == "https://api.anthropic.com/v1"
    assert resolved.api_key == "sk-ant-real-key-XYZ"
    assert resolved.temperature == 0.2
    assert resolved.max_tokens == 2048

    # 5. Dispatch the call — patch litellm.acompletion to capture kwargs.
    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _mock_litellm_response("answer")

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        response = await dispatch_assignment(
            assignment=resolved,
            messages=[{"role": "user", "content": "what is X?"}],
        )

    # 6. Verify response carried through.
    assert response.choices[0].message.content == "answer"

    # 7. Verify litellm.acompletion received every Endpoint + Assignment param.
    # PR15: dispatch now strips a matching ``<provider>/`` prefix and forwards
    # ``custom_llm_provider`` explicitly so LiteLLM can't silently route to
    # a different provider based on bare-model-string heuristics.
    assert captured_kwargs["model"] == "claude-sonnet-4-6"
    assert captured_kwargs["custom_llm_provider"] == "anthropic"
    assert captured_kwargs["api_base"] == "https://api.anthropic.com/v1"
    assert captured_kwargs["api_key"] == "sk-ant-real-key-XYZ"
    assert captured_kwargs["temperature"] == 0.2
    assert captured_kwargs["max_tokens"] == 2048
    assert captured_kwargs["messages"] == [{"role": "user", "content": "what is X?"}]


# ─── E2E #2: two-orgs scenario — independent throttle buckets ──────────


@pytest.mark.asyncio
async def test_e2e_two_openai_endpoints_throttle_independently() -> None:
    """Two OpenAI Endpoints (prod + staging) get independent throttle state."""
    stores = _stores()
    ep_store = EndpointStore(stores.mongodb)
    prod = await ep_store.create(
        name="OpenAI prod",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-prod",
        models=["gpt-4o-mini"],
    )
    staging = await ep_store.create(
        name="OpenAI staging",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-staging",
        models=["gpt-4o-mini"],
    )
    await hydrate_runtime_credentials(stores)

    # Two assignments — fact_extractor on prod, entity_extractor on staging.
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(consumer="fact_extractor", endpoint_id=prod.id, model="gpt-4o-mini")
    )
    await asn_store.upsert(
        Assignment(consumer="entity_extractor", endpoint_id=staging.id, model="gpt-4o-mini")
    )

    provider = LLMProvider(_settings_stub())
    r_prod = await provider.resolve_for_call("fact_extractor", stores=stores)
    r_staging = await provider.resolve_for_call("entity_extractor", stores=stores)
    assert r_prod.api_key == "sk-prod"  # type: ignore[union-attr]
    assert r_staging.api_key == "sk-staging"  # type: ignore[union-attr]
    assert r_prod.endpoint_id != r_staging.endpoint_id  # type: ignore[union-attr]

    # Dispatch both — verify the throttle saw distinct endpoint_id keys.
    from beever_atlas.services.llm_throttle import get_llm_throttle

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        return _mock_litellm_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_assignment(assignment=r_prod, messages=[{"role": "user", "content": "x"}])
        await dispatch_assignment(assignment=r_staging, messages=[{"role": "user", "content": "y"}])

    throttle = get_llm_throttle()
    bucket_keys = set(throttle._buckets.keys())
    # Two distinct per-Endpoint buckets — not a shared "openai" bucket.
    assert f"openai:{prod.id}" in bucket_keys
    assert f"openai:{staging.id}" in bucket_keys


# ─── E2E #3: failover routes to fallback when breaker opens ──────────────


@pytest.mark.asyncio
async def test_e2e_failover_routes_to_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the circuit breaker is open and a fallback is configured, dispatch
    routes the call to the fallback Endpoint."""
    stores = _stores()
    ep_store = EndpointStore(stores.mongodb)
    primary = await ep_store.create(
        name="primary",
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-ant-primary",
        models=["claude-sonnet-4-6"],
    )
    fallback = await ep_store.create(
        name="fallback",
        preset="google_ai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        auth_type="api_key",
        plaintext_credential="AIzaSy-fallback",
        models=["gemini-2.5-flash"],
    )
    await hydrate_runtime_credentials(stores)

    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="qa_agent",
            endpoint_id=primary.id,
            model="claude-sonnet-4-6",
            fallback_endpoint_id=fallback.id,
        )
    )

    # Trip the PRIMARY endpoint's breaker only — the fallback's stays closed.
    from beever_atlas.services.circuit_breaker import get_breaker_for_endpoint

    primary_breaker = get_breaker_for_endpoint(primary.id)
    for _ in range(primary_breaker._threshold):
        await primary_breaker.record_failure(RuntimeError("primary outage"))
    assert primary_breaker.is_open() is True

    provider = LLMProvider(_settings_stub())
    resolved = await provider.resolve_for_call("qa_agent", stores=stores)
    assert resolved is not None
    # Resolved to the FALLBACK endpoint. The fallback uses Google's OpenAI-compat
    # ``/openai/`` shim → routes through LiteLLM's ``openai`` provider (the
    # native ``gemini`` path 404s against the shim URL).
    assert resolved.endpoint_id == fallback.id
    assert resolved.api_key == "AIzaSy-fallback"
    assert resolved.provider == "openai"

    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _mock_litellm_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_assignment(assignment=resolved, messages=[{"role": "user", "content": "x"}])

    # Dispatch went to the fallback Endpoint's URL + key.
    assert captured_kwargs["api_base"] == fallback.base_url
    assert captured_kwargs["api_key"] == "AIzaSy-fallback"


# ─── E2E #4: failover raises when both circuits open ─────────────────────


@pytest.mark.asyncio
async def test_e2e_failover_raises_when_no_fallback_and_breaker_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stores = _stores()
    ep_store = EndpointStore(stores.mongodb)
    primary = await ep_store.create(
        name="primary",
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-ant",
        models=["claude-sonnet-4-6"],
    )
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(consumer="qa_agent", endpoint_id=primary.id, model="claude-sonnet-4-6")
    )

    from beever_atlas.services import circuit_breaker as cb_mod

    monkeypatch.setattr(cb_mod.CircuitBreaker, "is_open", lambda self: True)

    provider = LLMProvider(_settings_stub())
    with pytest.raises(CircuitBreakerOpenForBothPrimaryAndFallback):
        await provider.resolve_for_call("qa_agent", stores=stores)


# ─── E2E #5: caller kwargs override Assignment defaults ─────────────────


@pytest.mark.asyncio
async def test_e2e_caller_kwargs_override_assignment_params() -> None:
    """An agent that wants tight control over response_format / temperature
    can pass them at call time; the Assignment's defaults are overridden."""
    stores = _stores()
    ep_store = EndpointStore(stores.mongodb)
    endpoint = await ep_store.create(
        name="X",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-x",
        models=["gpt-4o-mini"],
    )
    await hydrate_runtime_credentials(stores)

    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="csv_mapper",
            endpoint_id=endpoint.id,
            model="gpt-4o-mini",
            temperature=0.5,  # default
        )
    )

    provider = LLMProvider(_settings_stub())
    resolved = await provider.resolve_for_call("csv_mapper", stores=stores)
    assert resolved is not None

    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _mock_litellm_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        # Caller overrides temperature.
        await dispatch_assignment(
            assignment=resolved,
            messages=[{"role": "user", "content": "x"}],
            temperature=0.0,  # override
        )

    # Override wins.
    assert captured_kwargs["temperature"] == 0.0


# ─── E2E #6: response_format json mode flows correctly ──────────────────


@pytest.mark.asyncio
async def test_e2e_response_format_json_translates_to_openai_shape() -> None:
    stores = _stores()
    ep_store = EndpointStore(stores.mongodb)
    endpoint = await ep_store.create(
        name="X",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-x",
        models=["gpt-4o-mini"],
    )
    await hydrate_runtime_credentials(stores)

    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="csv_mapper",
            endpoint_id=endpoint.id,
            model="gpt-4o-mini",
            response_format="json",
        )
    )

    provider = LLMProvider(_settings_stub())
    resolved = await provider.resolve_for_call("csv_mapper", stores=stores)
    assert resolved is not None

    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _mock_litellm_response('{"k": "v"}')

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_assignment(assignment=resolved, messages=[{"role": "user", "content": "x"}])

    assert captured_kwargs["response_format"] == {"type": "json_object"}


# ─── E2E #7: ollama endpoint with no auth ──────────────────────────────


@pytest.mark.asyncio
async def test_e2e_ollama_v1_no_auth_passes_placeholder_api_key() -> None:
    """Local Ollama Endpoints with ``/v1`` base_url have ``auth_type=none`` AND
    route through LiteLLM's ``openai`` provider (the OpenAI-compat shim). LiteLLM's
    ``openai`` provider rejects a missing api_key client-side even when the
    upstream server ignores it, so dispatch passes a harmless placeholder."""
    stores = _stores()
    ep_store = EndpointStore(stores.mongodb)
    endpoint = await ep_store.create(
        name="Ollama local",
        preset="ollama",
        base_url="http://localhost:11434/v1",
        auth_type="none",
        plaintext_credential=None,
        models=["qwen2.5:14b"],
    )
    await hydrate_runtime_credentials(stores)

    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="fact_extractor",
            endpoint_id=endpoint.id,
            model="qwen2.5:14b",
        )
    )

    provider = LLMProvider(_settings_stub())
    resolved = await provider.resolve_for_call("fact_extractor", stores=stores)
    assert resolved is not None
    # ``/v1`` shim ⇒ openai provider, bare model.
    assert resolved.provider == "openai"
    assert resolved.litellm_model == "qwen2.5:14b"
    assert resolved.api_key is None

    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _mock_litellm_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_assignment(assignment=resolved, messages=[{"role": "user", "content": "x"}])

    # Placeholder api_key injected — LiteLLM openai SDK rejects missing keys.
    assert captured_kwargs["api_key"] == "placeholder-no-auth"
    # api_base IS passed so LiteLLM routes to localhost.
    assert captured_kwargs["api_base"] == "http://localhost:11434/v1"
    assert captured_kwargs["model"] == "qwen2.5:14b"
    # PR15: ``custom_llm_provider`` is the load-bearing routing signal —
    # without it, LiteLLM 400s on ``qwen2.5:14b`` because no model registry
    # entry resolves it.
    assert captured_kwargs["custom_llm_provider"] == "openai"


# ─── E2E #8: extra_headers from Endpoint + Assignment merge ─────────────


@pytest.mark.asyncio
async def test_e2e_extra_headers_merge_endpoint_and_assignment() -> None:
    """Endpoint headers + Assignment extra_headers BOTH propagate, with
    Assignment headers taking precedence on collision."""
    stores = _stores()
    ep_store = EndpointStore(stores.mongodb)
    endpoint = await ep_store.create(
        name="custom",
        preset="custom",
        base_url="https://proxy/v1",
        auth_type="api_key",
        plaintext_credential="sk-x",
        models=["x"],
        headers={"X-Endpoint": "ep", "X-Both": "from-endpoint"},
    )
    await hydrate_runtime_credentials(stores)

    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="qa_agent",
            endpoint_id=endpoint.id,
            model="x",
            extra_headers={"X-Assignment": "asn", "X-Both": "from-assignment"},
        )
    )

    provider = LLMProvider(_settings_stub())
    resolved = await provider.resolve_for_call("qa_agent", stores=stores)
    assert resolved is not None

    captured_kwargs: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return _mock_litellm_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        await dispatch_assignment(assignment=resolved, messages=[{"role": "user", "content": "x"}])

    headers = captured_kwargs["extra_headers"]
    assert headers["X-Endpoint"] == "ep"
    assert headers["X-Assignment"] == "asn"
    # Assignment wins on collision.
    assert headers["X-Both"] == "from-assignment"


# ─── E2E #9: PUT capability validation blocks incompatible assignment ───


@pytest.mark.asyncio
async def test_e2e_put_blocks_qa_agent_on_non_tool_model() -> None:
    """``qa_agent`` + ``deepseek-reasoner`` → 422 with suggestions.

    Exercises the API layer's capability gate. The PUT request shape is
    documented in the assignment-overrides spec.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from beever_atlas.api import assignments as asn_api
    from beever_atlas.api import endpoints as ep_api

    stores = _stores()

    with (
        patch("beever_atlas.api.endpoints.get_stores", return_value=stores),
        patch("beever_atlas.api.assignments.get_stores", return_value=stores),
    ):
        app = FastAPI()
        app.include_router(ep_api.router)
        app.include_router(asn_api.router)
        client = TestClient(app)

        # Seed a DeepSeek + OpenAI Endpoint (suggestions need an alternative).
        ds = client.post(
            "/api/settings/endpoints",
            json={
                "name": "ds",
                "preset": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "auth_type": "api_key",
                "api_key": "ds-test",
                "models": ["deepseek-reasoner", "deepseek-chat"],
            },
        ).json()
        client.post(
            "/api/settings/endpoints",
            json={
                "name": "oai",
                "preset": "openai",
                "base_url": "https://api.openai.com/v1",
                "auth_type": "api_key",
                "api_key": "sk-oai",
                "models": ["gpt-4o-mini"],
            },
        )

        # PUT qa_agent → deepseek-reasoner → 422 with suggestions.
        resp = client.put(
            "/api/settings/assignments/qa_agent",
            json={"endpoint_id": ds["id"], "model": "deepseek-reasoner"},
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert detail["error"] == "incompatible_assignment"
        assert "tools" in detail["missing_capabilities"]
        assert len(detail["suggested"]) >= 1


# ─── E2E #10: preset apply → resolve → dispatch ─────────────────────────


@pytest.mark.asyncio
async def test_e2e_preset_apply_then_dispatch() -> None:
    """Full preset application → every consumer's dispatch path works."""
    stores = _stores()
    ep_store = EndpointStore(stores.mongodb)
    google = await ep_store.create(
        name="Google AI",
        preset="google_ai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        auth_type="api_key",
        plaintext_credential="AIzaSy-test",
        models=[
            "gemini-2.5-flash",
            "gemini-2.5-flash-lite",
            "gemini-embedding-001",
        ],
    )
    await hydrate_runtime_credentials(stores)

    # Apply the gemini-balanced preset.
    from beever_atlas.llm.presets import apply_preset

    endpoints = await ep_store.list()
    seed = apply_preset("gemini-balanced", endpoints)
    asn_store = AssignmentStore(stores.mongodb)
    for assignment in seed.values():
        await asn_store.upsert(assignment)

    # Every consumer can resolve.
    provider = LLMProvider(_settings_stub())
    for consumer in [
        "fact_extractor",
        "qa_agent",
        "wiki_compiler",
        "csv_mapper",
        "embedding",
    ]:
        resolved = await provider.resolve_for_call(consumer, stores=stores)
        assert resolved is not None, f"{consumer} did not resolve"
        assert resolved.endpoint_id == google.id
        assert resolved.api_key == "AIzaSy-test"

    # Dispatch every chat consumer (skip embedding — that's dispatch_embedding's
    # path; covered by the existing embedding tests).
    captured_models: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured_models.append(kwargs["model"])
        return _mock_litellm_response()

    with patch("litellm.acompletion", side_effect=fake_acompletion):
        for consumer in ["fact_extractor", "qa_agent", "wiki_compiler", "csv_mapper"]:
            resolved = await provider.resolve_for_call(consumer, stores=stores)
            assert resolved is not None
            await dispatch_assignment(
                assignment=resolved,
                messages=[{"role": "user", "content": "test"}],
            )

    # ``google_ai`` Endpoint with the ``/openai/`` shim base_url routes through
    # LiteLLM's ``openai`` provider with bare model ids (the shim 404s under
    # the native ``gemini/`` path).
    assert all(not m.startswith("gemini/") for m in captured_models), captured_models
    assert "gemini-2.5-flash" in captured_models  # bare, no provider prefix
    assert len(captured_models) == 4
