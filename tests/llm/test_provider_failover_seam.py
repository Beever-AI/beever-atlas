"""PR-H: failover seam wired through per-Assignment fallback_endpoint_id.

The legacy ``_FAILOVER_ENABLED`` / ``_FALLBACK_MAP`` module constants are gone
(see ``openspec/changes/agent-llm-provider-pluggable/design.md`` D14). Failover
is now driven by:
  * ``Assignment.fallback_endpoint_id`` — operator-configured per-consumer
  * ``circuit_breaker.is_open()`` — the existing global breaker state
  * ``LLMProvider.resolve_for_call(consumer)`` — the resolution entry point

Tests:
  * No fallback configured → primary returned regardless of breaker.
  * Breaker closed + fallback configured → primary returned.
  * Breaker open + fallback configured → fallback returned.
  * Breaker open + no fallback → CircuitBreakerOpenForBothPrimaryAndFallback.
  * Missing Assignment → resolve_for_call returns None (legacy fall-through).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from beever_atlas.llm.assignments import Assignment, AssignmentStore
from beever_atlas.llm.endpoints import EndpointStore
from beever_atlas.llm.provider import (
    CircuitBreakerOpenForBothPrimaryAndFallback,
    LLMProvider,
)
from beever_atlas.services.circuit_breaker import reset_circuit_breaker_for_tests


# ─── Fakes (inlined for isolation) ───────────────────────────────────────


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


def _make_settings() -> Any:
    return SimpleNamespace(
        llm_fast_model="gemini-2.5-flash",
        llm_quality_model="gemini-2.5-pro",
    )


def _make_stores() -> Any:
    return SimpleNamespace(
        mongodb=SimpleNamespace(
            db={"endpoints": _FakeCollection(), "llm_assignments": _FakeCollection()}
        )
    )


@pytest.fixture(autouse=True)
def _reset_breaker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset the global breaker singleton + provide a master key for encryption."""
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)
    reset_circuit_breaker_for_tests()


async def _seed(stores: Any) -> tuple[str, str]:
    """Create primary + fallback Endpoints; return their IDs."""
    ep_store = EndpointStore(stores.mongodb)
    primary = await ep_store.create(
        name="primary",
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-ant",
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
    return primary.id, fallback.id


@pytest.mark.asyncio
async def test_resolve_for_call_returns_primary_when_no_assignment() -> None:
    """No Assignment → resolve_for_call returns None (caller falls back to legacy)."""
    stores = _make_stores()
    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("qa_agent", stores=stores)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_for_call_returns_primary_when_breaker_closed() -> None:
    """Healthy circuit + Assignment → primary Endpoint resolved."""
    stores = _make_stores()
    primary_id, _fallback_id = await _seed(stores)
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(consumer="qa_agent", endpoint_id=primary_id, model="claude-sonnet-4-6")
    )

    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("qa_agent", stores=stores)
    assert result is not None
    assert result.endpoint_id == primary_id
    assert result.provider == "anthropic"
    assert result.litellm_model == "anthropic/claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_resolve_for_call_routes_to_fallback_when_breaker_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaker open + fallback configured → resolve returns fallback Endpoint."""
    stores = _make_stores()
    primary_id, fallback_id = await _seed(stores)
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="qa_agent",
            endpoint_id=primary_id,
            model="claude-sonnet-4-6",
            fallback_endpoint_id=fallback_id,
        )
    )

    # Trip the PRIMARY endpoint's breaker only (the fallback's stays closed).
    from beever_atlas.services.circuit_breaker import get_breaker_for_endpoint

    primary_breaker = get_breaker_for_endpoint(primary_id)
    for _ in range(primary_breaker._threshold):
        await primary_breaker.record_failure(RuntimeError("primary outage"))
    assert primary_breaker.is_open() is True

    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("qa_agent", stores=stores)
    assert result is not None
    assert result.endpoint_id == fallback_id
    # The fallback Endpoint's base_url is Google's OpenAI-compat ``/openai/``
    # shim, so the resolver routes it through LiteLLM's ``openai`` provider
    # with a bare model id (the shim 404s under the native ``gemini/`` path).
    assert result.provider == "openai"
    # Model name from Assignment is preserved on fallback (operator's choice).
    assert result.litellm_model == "claude-sonnet-4-6"
    # api_base honoured for OpenAI-compat routing.
    assert result.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"


@pytest.mark.asyncio
async def test_resolve_for_call_raises_when_breaker_open_and_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Breaker open + no fallback → CircuitBreakerOpenForBothPrimaryAndFallback."""
    stores = _make_stores()
    primary_id, _ = await _seed(stores)
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(consumer="qa_agent", endpoint_id=primary_id, model="claude-sonnet-4-6")
    )

    from beever_atlas.services import circuit_breaker as cb_mod

    monkeypatch.setattr(cb_mod.CircuitBreaker, "is_open", lambda self: True)

    provider = LLMProvider(_make_settings())
    with pytest.raises(CircuitBreakerOpenForBothPrimaryAndFallback) as exc:
        await provider.resolve_for_call("qa_agent", stores=stores)
    assert exc.value.consumer == "qa_agent"
    assert exc.value.primary_id == primary_id
    assert exc.value.fallback_id is None


@pytest.mark.asyncio
async def test_resolve_for_call_forwards_per_assignment_params() -> None:
    """The Assignment's temperature / max_tokens / response_format flow into
    the ResolvedAssignment."""
    stores = _make_stores()
    primary_id, _ = await _seed(stores)
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="csv_mapper",
            endpoint_id=primary_id,
            model="claude-haiku-4-5",
            temperature=0.0,
            max_tokens=1024,
            response_format="json",
        )
    )

    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("csv_mapper", stores=stores)
    assert result is not None
    assert result.temperature == 0.0
    assert result.max_tokens == 1024
    assert result.response_format == "json"


@pytest.mark.asyncio
async def test_resolve_for_call_combines_endpoint_and_assignment_headers() -> None:
    stores = _make_stores()
    ep_store = EndpointStore(stores.mongodb)
    primary = await ep_store.create(
        name="custom",
        preset="custom",
        base_url="https://proxy.internal/v1",
        auth_type="api_key",
        plaintext_credential="sk",
        models=["model-x"],
        headers={"X-Endpoint-Header": "yes"},
    )
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(
            consumer="fact_extractor",
            endpoint_id=primary.id,
            model="model-x",
            extra_headers={"X-Assignment-Header": "yes"},
        )
    )

    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("fact_extractor", stores=stores)
    assert result is not None
    assert result.extra_headers["X-Endpoint-Header"] == "yes"
    assert result.extra_headers["X-Assignment-Header"] == "yes"


@pytest.mark.asyncio
async def test_resolve_for_call_routes_google_ai_openai_compat_through_openai() -> None:
    """``google_ai`` Endpoint with the ``/openai/`` shim base_url → resolver
    returns ``provider=openai`` with a bare model id and the base_url honoured.
    Documents the routing rule that fixes the production ``GeminiException -
    NotFound`` bug — LiteLLM's native ``gemini`` provider expects Google's
    native API path, not the OpenAI-compat shim."""
    stores = _make_stores()
    ep_store = EndpointStore(stores.mongodb)
    google = await ep_store.create(
        name="google",
        preset="google_ai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        auth_type="api_key",
        plaintext_credential="AIza-key",
        models=["models/gemini-2.5-flash"],
    )
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(consumer="qa_agent", endpoint_id=google.id, model="gemini-2.5-flash")
    )

    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("qa_agent", stores=stores)
    assert result is not None
    assert result.provider == "openai"
    assert result.litellm_model == "gemini-2.5-flash"
    assert result.base_url == "https://generativelanguage.googleapis.com/v1beta/openai/"


@pytest.mark.asyncio
async def test_resolve_for_call_routes_google_ai_native_drops_base_url() -> None:
    """``google_ai`` Endpoint with no base_url → resolver returns the native
    ``gemini`` provider and DROPS the api_base (LiteLLM's ``gemini`` provider
    routes through Google's default host and breaks if api_base is set)."""
    stores = _make_stores()
    ep_store = EndpointStore(stores.mongodb)
    google = await ep_store.create(
        name="google-native",
        preset="google_ai",
        base_url="",
        auth_type="api_key",
        plaintext_credential="AIza-key",
        models=["gemini-2.5-flash"],
    )
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(consumer="qa_agent", endpoint_id=google.id, model="gemini-2.5-flash")
    )

    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("qa_agent", stores=stores)
    assert result is not None
    assert result.provider == "gemini"
    assert result.litellm_model == "gemini/gemini-2.5-flash"
    assert result.base_url is None


@pytest.mark.asyncio
async def test_resolve_for_call_routes_ollama_v1_through_openai() -> None:
    """``ollama`` Endpoint with ``/v1`` base_url → resolver returns
    ``provider=openai`` with a bare model id (the OpenAI-compat shim accepts
    ``/v1/chat/completions``; LiteLLM's ``ollama_chat`` POSTs to ``/api/chat``)."""
    stores = _make_stores()
    ep_store = EndpointStore(stores.mongodb)
    ollama = await ep_store.create(
        name="ollama-shim",
        preset="ollama",
        base_url="http://localhost:11434/v1",
        auth_type="none",
        plaintext_credential=None,
        models=["gemma4:e2b"],
    )
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(consumer="fact_extractor", endpoint_id=ollama.id, model="gemma4:e2b")
    )

    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("fact_extractor", stores=stores)
    assert result is not None
    assert result.provider == "openai"
    assert result.litellm_model == "gemma4:e2b"
    assert result.base_url == "http://localhost:11434/v1"


@pytest.mark.asyncio
async def test_resolve_for_call_routes_ollama_native_keeps_ollama_chat() -> None:
    """``ollama`` Endpoint with the native base_url → ``provider=ollama_chat``
    with the prefixed model id (native ``/api/chat`` path)."""
    stores = _make_stores()
    ep_store = EndpointStore(stores.mongodb)
    ollama = await ep_store.create(
        name="ollama-native",
        preset="ollama",
        base_url="http://localhost:11434",
        auth_type="none",
        plaintext_credential=None,
        models=["llama3.2:latest"],
    )
    asn_store = AssignmentStore(stores.mongodb)
    await asn_store.upsert(
        Assignment(consumer="fact_extractor", endpoint_id=ollama.id, model="llama3.2:latest")
    )

    provider = LLMProvider(_make_settings())
    result = await provider.resolve_for_call("fact_extractor", stores=stores)
    assert result is not None
    assert result.provider == "ollama_chat"
    assert result.litellm_model == "ollama_chat/llama3.2:latest"
    assert result.base_url == "http://localhost:11434"


@pytest.mark.asyncio
async def test_legacy_failover_constants_removed() -> None:
    """The legacy ``_FAILOVER_ENABLED`` / ``_FALLBACK_MAP`` module constants
    are gone — design D14 explicitly removes them."""
    import beever_atlas.llm.provider as provider_mod

    assert not hasattr(provider_mod, "_FAILOVER_ENABLED")
    assert not hasattr(provider_mod, "_FALLBACK_MAP")
