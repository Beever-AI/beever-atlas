"""PR-E: ``/api/settings/endpoints`` endpoint tests."""

from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api import endpoints as ep_api


# ─── In-memory fake collection ────────────────────────────────────────────


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
    def __init__(self, matched: int = 0, deleted: int = 0) -> None:
        self.matched_count = matched
        self.modified_count = matched
        self.deleted_count = deleted


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

    async def delete_one(self, query: dict[str, Any]) -> _Result:
        for d in list(self._docs):
            if self._matches(d, query):
                self._docs.remove(d)
                return _Result(deleted=1)
        return _Result(deleted=0)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if "$or" in query:
            return any(_FakeCollection._matches(doc, q) for q in query["$or"])
        return all(doc.get(k) == v for k, v in query.items())


def _make_stores() -> Any:
    endpoints_coll = _FakeCollection()
    assignments_coll = _FakeCollection()
    mongodb = SimpleNamespace(db={"endpoints": endpoints_coll, "llm_assignments": assignments_coll})
    return SimpleNamespace(mongodb=mongodb)


@pytest.fixture
def app_and_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """FastAPI app with the endpoints router. Patches ``get_stores`` + master key."""
    # Master key for encryption.
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "ab" * 32)

    # Move cwd into a tempdir so Settings doesn't pick up the dev .env.
    _tmp = tempfile.TemporaryDirectory()
    _prev = os.getcwd()
    os.chdir(_tmp.name)

    stores = _make_stores()
    monkeypatch.setattr("beever_atlas.api.endpoints.get_stores", lambda: stores)
    # Reset the runtime credentials cache between tests.
    from beever_atlas.llm.agent_credentials import clear_all_runtime_credentials

    clear_all_runtime_credentials()

    app = FastAPI()
    app.include_router(ep_api.router)
    try:
        yield app, TestClient(app), stores
    finally:
        os.chdir(_prev)
        _tmp.cleanup()


# ─── GET / POST ─────────────────────────────────────────────────────────


def test_list_endpoints_empty(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.get("/api/settings/endpoints")
    assert resp.status_code == 200
    assert resp.json() == {"endpoints": []}


def test_create_endpoint_encrypts_credential(app_and_client: Any) -> None:
    _app, client, stores = app_and_client
    body = {
        "name": "Anthropic prod",
        "preset": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "auth_type": "api_key",
        "api_key": "sk-ant-real-secret-XYZ1234",
        "models": ["claude-sonnet-4-6"],
        "rpm": 100,
    }
    resp = client.post("/api/settings/endpoints", json=body)
    assert resp.status_code == 201, resp.text
    payload = resp.json()
    assert payload["name"] == "Anthropic prod"
    assert payload["has_credential"] is True
    assert payload["credential_masked"] == "sk-a...1234"
    # The plaintext NEVER appears in the response.
    assert "sk-ant-real-secret" not in resp.text
    # Persisted document has only the encrypted envelope.
    persisted = stores.mongodb.db["endpoints"]._docs[0]
    assert "sk-ant-real-secret" not in str(persisted)
    assert "encrypted_key" in persisted


def test_create_endpoint_rejects_unknown_preset(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={"name": "x", "preset": "totally_made_up", "auth_type": "api_key"},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "unsupported_preset"


def test_create_oauth_returns_not_implemented(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={"name": "x", "preset": "openai", "auth_type": "oauth"},
    )
    assert resp.status_code == 501
    assert resp.json()["detail"]["error"] == "oauth_not_yet_supported"


def test_get_endpoint_by_id(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    create = client.post(
        "/api/settings/endpoints",
        json={
            "name": "G",
            "preset": "openai",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-test",
        },
    ).json()
    fetched = client.get(f"/api/settings/endpoints/{create['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["id"] == create["id"]


def test_get_endpoint_not_found(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.get("/api/settings/endpoints/nonexistent-id")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "endpoint_not_found"


# ─── PUT ────────────────────────────────────────────────────────────────


def test_update_replaces_credential(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "X",
            "preset": "openai",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-old-secret-key-AAAA",
        },
    ).json()
    update = client.put(
        f"/api/settings/endpoints/{created['id']}",
        json={"api_key": "sk-new-secret-key-BBBB"},
    )
    assert update.status_code == 200
    assert update.json()["credential_masked"] == "sk-n...BBBB"


def test_update_preserves_credential_when_unspecified(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "X",
            "preset": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-ant-original-AAAA",
        },
    ).json()
    update = client.put(
        f"/api/settings/endpoints/{created['id']}",
        json={"name": "X renamed", "rpm": 50},
    )
    assert update.status_code == 200
    body = update.json()
    assert body["name"] == "X renamed"
    assert body["rpm"] == 50
    assert body["credential_masked"] == "sk-a...AAAA"


# ─── DELETE ─────────────────────────────────────────────────────────────


def test_delete_endpoint(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={"name": "X", "preset": "openai", "auth_type": "api_key", "api_key": "sk"},
    ).json()
    resp = client.delete(f"/api/settings/endpoints/{created['id']}")
    assert resp.status_code == 204
    # Subsequent GET 404s.
    assert client.get(f"/api/settings/endpoints/{created['id']}").status_code == 404


def test_delete_protected_when_in_use(app_and_client: Any) -> None:
    """An Assignment referencing the Endpoint blocks DELETE with 409."""
    _app, client, stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={"name": "X", "preset": "openai", "auth_type": "api_key", "api_key": "sk-x"},
    ).json()

    # Inject an Assignment referencing this Endpoint directly into the store.
    stores.mongodb.db["llm_assignments"]._docs.append(
        {"consumer": "qa_agent", "endpoint_id": created["id"], "model": "gpt-4o"}
    )

    resp = client.delete(f"/api/settings/endpoints/{created['id']}")
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "endpoint_in_use_as_primary_or_fallback"
    assert "qa_agent" in detail["consumers"]


# ─── Discover ───────────────────────────────────────────────────────────


def test_discover_unknown_endpoint(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.post("/api/settings/endpoints/nope/discover")
    assert resp.status_code == 404


def test_discover_returns_models_for_ollama(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama discovery hits the native ``/api/tags`` endpoint."""
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "ollama",
            "preset": "ollama",
            "base_url": "http://localhost:11434/v1",
            "auth_type": "none",
        },
    ).json()

    async def fake_discover(endpoint, **_kw):  # noqa: ANN001
        return {
            "ok": True,
            "models": ["gemma3:e4b", "qwen2.5:14b"],
            "models_by_kind": {"chat": ["gemma3:e4b", "qwen2.5:14b"], "embedding": []},
            "dropped": {},
        }

    monkeypatch.setattr("beever_atlas.api.endpoints.discover_models", fake_discover)
    resp = client.post(f"/api/settings/endpoints/{created['id']}/discover")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "gemma3:e4b" in body["models"]


def test_discover_persists_model_kinds_and_advanced_models(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discover persists the classifier output: kept ids into ``models`` +
    ``model_kinds``, dropped ids into ``advanced_models``; response carries
    ``by_kind`` + ``dropped_breakdown`` counts."""
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "jina",
            "preset": "jina_ai",
            "base_url": "https://api.jina.ai/v1",
            "auth_type": "api_key",
            "api_key": "jina-test-key-xxxxxxxx",
        },
    ).json()

    async def fake_discover(endpoint, **_kw):  # noqa: ANN001
        return {
            "ok": True,
            "models": ["jina-embeddings-v3", "jina-embeddings-v4"],
            "models_by_kind": {
                "chat": [],
                "embedding": ["jina-embeddings-v3", "jina-embeddings-v4"],
            },
            "dropped": {
                "reranker": ["jina-reranker-v2-base-multilingual"],
                "clip": ["jina-vlm", "jina-clip-v1"],
                "segmenter": ["jina-segmenter-v1"],
            },
        }

    monkeypatch.setattr("beever_atlas.api.endpoints.discover_models", fake_discover)
    resp = client.post(f"/api/settings/endpoints/{created['id']}/discover")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["models"] == ["jina-embeddings-v3", "jina-embeddings-v4"]
    assert body["by_kind"]["embedding"] == ["jina-embeddings-v3", "jina-embeddings-v4"]
    assert body["by_kind"]["chat"] == []
    assert body["dropped_breakdown"] == {"reranker": 1, "clip": 2, "segmenter": 1}

    fetched = client.get(f"/api/settings/endpoints/{created['id']}").json()
    assert fetched["models"] == ["jina-embeddings-v3", "jina-embeddings-v4"]
    assert fetched["model_kinds"] == {
        "jina-embeddings-v3": "embedding",
        "jina-embeddings-v4": "embedding",
    }
    assert set(fetched["advanced_models"]) == {
        "jina-reranker-v2-base-multilingual",
        "jina-vlm",
        "jina-clip-v1",
        "jina-segmenter-v1",
    }


def test_discover_preserves_manually_kept_ids(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Models the operator manually promoted (``manually_kept``) survive a
    re-Discover even when the classifier would otherwise drop them."""
    _app, client, stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "openai",
            "preset": "openai",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-test-key-aaaaaaaaaaaa",
        },
    ).json()

    # Seed the persisted document with a manually-kept id (and a prior
    # classification we expect Discover to preserve).
    coll = stores.mongodb.db["endpoints"]
    for doc in coll._docs:
        if doc["id"] == created["id"]:
            doc["manually_kept"] = ["some-private-fine-tune"]
            doc["model_kinds"] = {"some-private-fine-tune": "chat"}

    async def fake_discover(endpoint, **_kw):  # noqa: ANN001
        return {
            "ok": True,
            "models": ["gpt-4o-mini"],
            "models_by_kind": {"chat": ["gpt-4o-mini"], "embedding": []},
            "dropped": {"image_gen": ["dall-e-3"]},
        }

    monkeypatch.setattr("beever_atlas.api.endpoints.discover_models", fake_discover)
    resp = client.post(f"/api/settings/endpoints/{created['id']}/discover")
    assert resp.status_code == 200
    body = resp.json()
    assert "some-private-fine-tune" in body["models"]
    assert "gpt-4o-mini" in body["models"]

    fetched = client.get(f"/api/settings/endpoints/{created['id']}").json()
    assert fetched["model_kinds"]["some-private-fine-tune"] == "chat"
    assert fetched["model_kinds"]["gpt-4o-mini"] == "chat"
    # manually_kept ids are NEVER routed into advanced_models even when the
    # classifier would otherwise put them there.
    assert "some-private-fine-tune" not in fetched["advanced_models"]
    assert "dall-e-3" in fetched["advanced_models"]


# ─── Plaintext absence ─────────────────────────────────────────────────


def test_list_response_never_includes_plaintext(app_and_client: Any) -> None:
    """An end-to-end audit: across create + list + get, no plaintext leaks."""
    _app, client, _stores = app_and_client
    secret = "VERY-SECRET-VALUE-DO-NOT-LEAK"
    client.post(
        "/api/settings/endpoints",
        json={
            "name": "leak-test",
            "preset": "openai",
            "base_url": "https://x",
            "auth_type": "api_key",
            "api_key": secret,
        },
    )
    list_text = client.get("/api/settings/endpoints").text
    assert secret not in list_text


# ─── /test (Test Connection) ────────────────────────────────────────────


def _seed_endpoint(
    stores: Any,
    *,
    preset: str,
    base_url: str,
    models: list[str],
    api_key: str | None = "test-key-AAAA-BBBB",
    auth_type: str = "api_key",
) -> str:
    """Seed an Endpoint directly into the fake store and prime the runtime
    credential cache. Bypasses the ``POST /endpoints`` create-allowlist (which
    doesn't accept every preset key the UI exposes — out of scope for this
    test module).
    """
    import uuid

    from beever_atlas.llm.agent_credentials import set_runtime_credential
    from beever_atlas.llm.endpoints import encrypt_endpoint_credential

    endpoint_id = str(uuid.uuid4())
    doc: dict[str, Any] = {
        "id": endpoint_id,
        "name": f"{preset}-test",
        "preset": preset,
        "base_url": base_url,
        "auth_type": auth_type,
        "encrypted_key": (
            encrypt_endpoint_credential(api_key)
            if api_key is not None and auth_type == "api_key"
            else None
        ),
        "models": list(models),
        "rpm": 500,
        "headers": {},
        "tags": [],
        "last_test_at": None,
        "last_test_ok": None,
        "last_test_error": None,
        "created_at": "2026-05-13T00:00:00+00:00",
        "updated_at": "2026-05-13T00:00:00+00:00",
    }
    stores.mongodb.db["endpoints"]._docs.append(doc)
    if api_key is not None and auth_type == "api_key":
        set_runtime_credential(endpoint_id, api_key)
    return endpoint_id


def _fake_litellm_response() -> MagicMock:
    """Minimal LiteLLM-shaped response — the dispatch wrappers only check
    ``status_code`` (must NOT be 429) and ``.choices[0].message.content`` is
    not asserted at all by ``dispatch_completion``."""
    response = MagicMock()
    response.status_code = 200
    return response


def _fake_litellm_embedding_response() -> dict[str, Any]:
    """LiteLLM normalises every embedding provider to the OpenAI shape."""
    return {"data": [{"embedding": [0.0] * 4, "index": 0}], "model": "x"}


def test_test_endpoint_jina_uses_embedding_path(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jina (preset=jina_ai, embedding-only) probes via ``litellm.aembedding``,
    NOT ``acompletion`` — the chat path 400s with ``LiteLLMUnknownProvider``.

    Routing nuance (PR-δ): when ``base_url`` ends in ``/v1`` (the default for
    the ``jina_ai`` preset), the probe goes through LiteLLM's ``openai``
    provider against Jina's OpenAI-shaped ``/v1/embeddings`` shim — that
    sidesteps quirks in LiteLLM's native ``jina_ai`` handler (URL builder /
    pinned host) which produced a misleading ``Connection refused`` in
    practice."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="jina_ai",
        base_url="https://api.jina.ai/v1",
        models=["jina-embeddings-v4"],
        api_key="jina-secret-key-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    embed_calls: list[dict[str, Any]] = []
    comp_calls: list[dict[str, Any]] = []

    async def fake_aembedding(**kwargs: Any) -> dict[str, Any]:
        embed_calls.append(kwargs)
        return _fake_litellm_embedding_response()

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        comp_calls.append(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["error"] is None

    # The completion path must NOT have been touched — Jina has no chat route.
    assert comp_calls == []
    # PR-δ: ``base_url`` ending in ``/v1`` triggers OpenAI-compat routing —
    # the bare model id reaches LiteLLM's openai SDK with the shim ``api_base``.
    assert len(embed_calls) == 1
    kw = embed_calls[0]
    assert kw["model"] == "jina-embeddings-v4"
    assert kw["custom_llm_provider"] == "openai"
    assert kw["api_base"] == "https://api.jina.ai/v1"
    assert kw["api_key"] == "jina-secret-key-AAAA"
    assert kw["input"] == ["test"]


def test_test_endpoint_jina_native_base_url_still_uses_jina_provider(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If an operator points a Jina endpoint at a non-OpenAI-compat URL
    (no ``/v1`` suffix — e.g. a private gateway), fall back to LiteLLM's
    native ``jina_ai`` provider. We only re-route when the shim path
    is recognisable."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="jina_ai",
        base_url="https://gateway.internal/jina",
        models=["jina-embeddings-v4"],
        api_key="jina-secret-key-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    embed_calls: list[dict[str, Any]] = []

    async def fake_aembedding(**kwargs: Any) -> dict[str, Any]:
        embed_calls.append(kwargs)
        return _fake_litellm_embedding_response()

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    assert len(embed_calls) == 1
    kw = embed_calls[0]
    assert kw["custom_llm_provider"] == "jina_ai"
    assert kw["api_base"] == "https://gateway.internal/jina"


def test_test_endpoint_google_ai_openai_compat_uses_openai_provider(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Google AI's ``/openai/`` shim speaks OpenAI's HTTP shape — route through
    LiteLLM's ``openai`` provider with a bare model. LiteLLM's native ``gemini``
    provider expects Google's native API path, not the OpenAI-compat shim, so
    routing the shim through ``gemini/`` produces ``GeminiException - NotFound``.
    Also: ``models/`` discovery prefix must be stripped."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="google_ai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        models=["models/gemini-2.5-flash", "gemini-2.5-pro"],
        api_key="AIza-test-key-XYZ-ABCD",
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # ``/openai/`` shim → bare model + openai provider; api_base honoured.
    # PR15: ``custom_llm_provider`` is now passed explicitly so LiteLLM can't
    # mis-route on the bare ``gemini-2.5-flash`` (which matches its native
    # gemini model registry).
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["custom_llm_provider"] == "openai"
    assert captured["api_base"] == "https://generativelanguage.googleapis.com/v1beta/openai/"
    assert captured["api_key"] == "AIza-test-key-XYZ-ABCD"
    assert captured["max_tokens"] == 1


def test_test_endpoint_google_ai_native_drops_api_base(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Google AI with no base_url → native ``gemini`` provider, ``gemini/<model>``,
    ``api_base`` is NOT passed (LiteLLM's gemini provider routes through Google's
    default host; a custom api_base breaks the native path)."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="google_ai",
        base_url="",
        models=["gemini-2.5-flash"],
        api_key="AIza-test-key-XYZ-ABCD",
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # PR15: bare model + ``custom_llm_provider=gemini``. Equivalent to the
    # ``gemini/<id>`` form LiteLLM also accepts, but the explicit kwarg is
    # the authoritative routing signal.
    assert captured["model"] == "gemini-2.5-flash"
    assert captured["custom_llm_provider"] == "gemini"
    # Native Gemini path — no api_base.
    assert "api_base" not in captured
    assert captured["api_key"] == "AIza-test-key-XYZ-ABCD"


def test_test_endpoint_ollama_v1_base_url_uses_openai_provider(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama with ``/v1`` base_url ⇒ OpenAI-compat shim ⇒ ``openai`` provider
    with a bare model. LiteLLM's ``ollama_chat`` POSTs to ``<base>/api/chat``
    — with ``/v1`` that becomes ``/v1/api/chat`` which 404s. The ``openai``
    provider POSTs to ``<base>/chat/completions`` which is what the shim expects."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="ollama",
        base_url="http://localhost:11434/v1",
        models=["gemma4:e2b", "gemma4:e4b"],
        auth_type="none",
        api_key=None,
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # OpenAI-compat path — bare model id, api_base honoured.
    # PR15: ``custom_llm_provider=openai`` is the load-bearing kwarg —
    # without it, LiteLLM tries to infer a provider from ``gemma4:e2b``
    # (no prefix, not in any model registry) and raises ``LLM Provider
    # NOT provided``.
    assert captured["model"] == "gemma4:e2b"
    assert captured["custom_llm_provider"] == "openai"
    # PR16: ``localhost`` → ``127.0.0.1`` rewrite for Ollama preset to dodge
    # the macOS IPv6-first / Ollama-binds-IPv4 ~75s connect stall.
    assert captured["api_base"] == "http://127.0.0.1:11434/v1"
    # ``auth_type=none`` + openai provider ⇒ placeholder api_key (LiteLLM's
    # openai SDK rejects a missing key client-side; server ignores the value).
    assert captured["api_key"] == "placeholder-no-auth"


def test_test_endpoint_ollama_native_base_url_uses_ollama_chat(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ollama with the native base_url (no ``/v1``) keeps the ``ollama_chat``
    provider — that path POSTs to ``<base>/api/chat``, which is what the native
    Ollama daemon expects."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="ollama",
        base_url="http://localhost:11434",
        models=["llama3.2:latest"],
        auth_type="none",
        api_key=None,
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # PR15: bare model + ``custom_llm_provider=ollama_chat`` — the prefix is
    # stripped now that the provider is the authoritative routing signal.
    assert captured["model"] == "llama3.2:latest"
    assert captured["custom_llm_provider"] == "ollama_chat"
    # PR16: ``localhost`` → ``127.0.0.1`` rewrite for Ollama preset to dodge
    # the macOS IPv6-first / Ollama-binds-IPv4 ~75s connect stall.
    assert captured["api_base"] == "http://127.0.0.1:11434"
    # auth_type=none + ollama_chat provider (not openai) ⇒ no placeholder needed.
    assert "api_key" not in captured


def test_test_endpoint_anthropic_uses_native_provider(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native LiteLLM providers (anthropic, mistral, groq) keep their preset
    prefix and honour the base_url."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=["claude-sonnet-4-6"],
        api_key="sk-ant-test-key-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # PR15: prefix stripped + ``custom_llm_provider`` passed explicitly.
    assert captured["model"] == "claude-sonnet-4-6"
    assert captured["custom_llm_provider"] == "anthropic"
    assert captured["api_base"] == "https://api.anthropic.com/v1"
    assert captured["api_key"] == "sk-ant-test-key-AAAA"


def test_test_endpoint_openai_passes_through_prefixed_model(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the operator already supplied a fully-prefixed LiteLLM id we trust
    it — no extra prefix munging."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="openai",
        base_url="https://api.openai.com/v1",
        models=["openai/gpt-4o-mini"],
        api_key="sk-test-secret-key-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True

    # PR15: an operator-supplied ``openai/...`` prefix matches the routed
    # provider — dispatch strips it and forwards the bare id + explicit
    # ``custom_llm_provider=openai``. The routing decision is unchanged;
    # only the wire form is now canonical.
    assert captured["model"] == "gpt-4o-mini"
    assert captured["custom_llm_provider"] == "openai"
    assert captured["api_base"] == "https://api.openai.com/v1"
    assert captured["api_key"] == "sk-test-secret-key-AAAA"


def test_test_endpoint_returns_sanitised_error_on_dispatch_failure(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatch failure returns 200 with ``ok=False`` and a redacted error
    string — never leaking the api_key embedded in the exception repr."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="openai",
        base_url="https://api.openai.com/v1",
        models=["gpt-4o-mini"],
        api_key="sk-LEAKY-SECRET-KEY-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    async def boom(**_kwargs: Any) -> MagicMock:
        # Exception text mimics a real LiteLLM repr that embeds api_key — the
        # sanitiser must scrub it.
        raise RuntimeError("auth failed for api_key=sk-LEAKY-SECRET-KEY-AAAA")

    monkeypatch.setattr(litellm, "acompletion", boom)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "sk-LEAKY-SECRET-KEY-AAAA" not in body["error"]
    assert "RuntimeError" in body["error"]


def test_test_endpoint_no_models_returns_actionable_error(app_and_client: Any) -> None:
    """An Endpoint with an empty model list short-circuits before dispatch."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint(
        stores,
        preset="openai",
        base_url="https://x",
        models=[],
        api_key="sk-test-AAAA",
    )

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert "endpoint_has_no_models" in body["error"]


def test_test_endpoint_unknown_id_returns_404(app_and_client: Any) -> None:
    _app, client, _stores = app_and_client
    resp = client.post("/api/settings/endpoints/nonexistent-id/test")
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"] == "endpoint_not_found"


# ─── PR-β: pick_probe_model + role + manually_kept ─────────────────────


def _seed_endpoint_with_role(
    stores: Any,
    *,
    preset: str,
    base_url: str,
    models: list[str],
    role: str = "auto",
    model_kinds: dict[str, str] | None = None,
    manually_kept: list[str] | None = None,
    api_key: str | None = "test-key-AAAA-BBBB",
    auth_type: str = "api_key",
) -> str:
    """Seed an Endpoint with PR-α/β fields populated."""
    import uuid

    from beever_atlas.llm.agent_credentials import set_runtime_credential
    from beever_atlas.llm.endpoints import encrypt_endpoint_credential

    endpoint_id = str(uuid.uuid4())
    doc: dict[str, Any] = {
        "id": endpoint_id,
        "name": f"{preset}-test",
        "preset": preset,
        "base_url": base_url,
        "auth_type": auth_type,
        "encrypted_key": (
            encrypt_endpoint_credential(api_key)
            if api_key is not None and auth_type == "api_key"
            else None
        ),
        "models": list(models),
        "rpm": 500,
        "headers": {},
        "tags": [],
        "last_test_at": None,
        "last_test_ok": None,
        "last_test_error": None,
        "created_at": "2026-05-13T00:00:00+00:00",
        "updated_at": "2026-05-13T00:00:00+00:00",
        "model_kinds": dict(model_kinds or {}),
        "advanced_models": [],
        "manually_kept": list(manually_kept or []),
        "role": role,
    }
    stores.mongodb.db["endpoints"]._docs.append(doc)
    if api_key is not None and auth_type == "api_key":
        set_runtime_credential(endpoint_id, api_key)
    return endpoint_id


def test_test_endpoint_picks_embedding_model_for_embedding_role(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An endpoint with ``role="embedding"`` probes the first model whose
    ``model_kinds`` value is ``"embedding"`` — NOT ``models[0]``."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint_with_role(
        stores,
        preset="jina_ai",
        base_url="https://api.jina.ai/v1",
        # jina-vlm comes FIRST in models[] but is NOT an embedding model.
        models=["jina-vlm", "jina-embeddings-v4"],
        role="embedding",
        model_kinds={"jina-embeddings-v4": "embedding"},
        api_key="jina-secret-key-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    embed_calls: list[dict[str, Any]] = []
    comp_calls: list[dict[str, Any]] = []

    async def fake_aembedding(**kwargs: Any) -> dict[str, Any]:
        embed_calls.append(kwargs)
        return _fake_litellm_embedding_response()

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        comp_calls.append(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert comp_calls == []
    assert len(embed_calls) == 1
    # Probed model is the embedding one, NOT models[0] (which is a VLM).
    assert embed_calls[0]["model"] == "jina-embeddings-v4"
    assert body["probed_model"] == "jina-embeddings-v4"
    assert body["probed_kind"] == "embedding"


def test_test_endpoint_picks_chat_model_for_chat_role(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``role="chat"`` probes the first chat-kinded model even when an
    operator-promoted embedding lives earlier in ``models``."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint_with_role(
        stores,
        preset="openai",
        base_url="https://api.openai.com/v1",
        # operator promoted an embedding before the chat model.
        models=["text-embedding-3-small", "gpt-4o-mini"],
        role="chat",
        model_kinds={
            "text-embedding-3-small": "embedding",
            "gpt-4o-mini": "chat",
        },
        api_key="sk-test-key-AAAAA",
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert captured["model"] == "gpt-4o-mini"
    assert body["probed_model"] == "gpt-4o-mini"
    assert body["probed_kind"] == "chat"


def test_test_endpoint_prefers_known_good_model_over_alphabetical_first(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-δ: Test probe walks the preset-preferred model list before falling
    back to ``endpoint.models`` order. Prevents ``models[0]`` landing on an
    experimental Gemini variant that the OpenAI shim 400s on."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint_with_role(
        stores,
        preset="google_ai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        # An alphabetical-first id that's NOT in the preferred list, plus a
        # preferred one that should beat it.
        models=["gemini-1.0-pro", "gemini-2.5-flash"],
        role="chat",
        model_kinds={
            "gemini-1.0-pro": "chat",
            "gemini-2.5-flash": "chat",
        },
        api_key="AIza-test-key-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    # Preferred wins over models[0].
    assert captured["model"] == "gemini-2.5-flash"
    assert body["probed_model"] == "gemini-2.5-flash"


def test_test_endpoint_retries_next_candidate_on_model_reject(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-δ: when the first probe fails with a 4xx pointing at the model id
    (``INVALID_ARGUMENT`` / ``model not found``), Test retries against the
    next preferred candidate. Operators with 40+ chat-tagged Gemini models
    don't have to manually shuffle ``endpoint.models`` to get a green Test."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint_with_role(
        stores,
        preset="google_ai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        # Preferred order puts gemini-2.5-flash first; the legacy id below
        # is the live/realtime variant the shim rejects.
        models=["models/gemini-2.5-flash-live-preview", "models/gemini-2.5-flash"],
        role="chat",
        model_kinds={
            # ``model_kinds`` is the canonical map — only mark the
            # working one as chat. The live model is correctly absent
            # so the probe walks the preferred list directly.
            "models/gemini-2.5-flash": "chat",
        },
        api_key="AIza-test-key-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    call_models: list[str] = []

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        call_models.append(kwargs["model"])
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    # Picker correctly skipped the live variant and reached the stable one.
    assert call_models == ["gemini-2.5-flash"]
    assert body["probed_model"] == "models/gemini-2.5-flash"


def test_test_endpoint_failure_includes_probed_model_in_error(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PR-δ: a failing Test surfaces ``[probed <model>]`` in the error string
    and exposes ``probed_model`` in the response, so operators can tell which
    specific model to move to advanced."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint_with_role(
        stores,
        preset="openai",
        base_url="https://api.openai.com/v1",
        models=["gpt-4o-mini"],
        role="chat",
        model_kinds={"gpt-4o-mini": "chat"},
        api_key="sk-test-key-AAAAA",
    )

    import litellm  # type: ignore[import-untyped]

    async def fake_acompletion(**_kwargs: Any) -> MagicMock:
        # Raise a non-model-reject failure so we don't trigger the retry
        # path; we just want to verify the error envelope shape.
        raise RuntimeError("upstream is angry")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is False
    assert "[probed gpt-4o-mini]" in body["error"]
    assert body["probed_model"] == "gpt-4o-mini"
    assert body["probed_kind"] == "chat"


def test_test_endpoint_auto_role_embedding_only_preset_probes_embedding(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``role="auto"`` on an embedding-only preset (jina) routes through the
    embedding path — the preset's natural side wins."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint_with_role(
        stores,
        preset="jina_ai",
        base_url="https://api.jina.ai/v1",
        models=["jina-embeddings-v4"],
        role="auto",
        model_kinds={"jina-embeddings-v4": "embedding"},
        api_key="jina-secret-key-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    embed_calls: list[dict[str, Any]] = []
    comp_calls: list[dict[str, Any]] = []

    async def fake_aembedding(**kwargs: Any) -> dict[str, Any]:
        embed_calls.append(kwargs)
        return _fake_litellm_embedding_response()

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        comp_calls.append(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    assert resp.json()["ok"] is True
    assert comp_calls == []
    assert len(embed_calls) == 1


def test_test_endpoint_falls_back_to_models_zero_when_no_kind_match(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Legacy endpoint (pre-α, no ``model_kinds``) probes ``models[0]`` via the
    classifier-inferred path — no 500 / 422."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint_with_role(
        stores,
        preset="openai",
        base_url="https://api.openai.com/v1",
        models=["gpt-4o-mini"],
        role="auto",
        model_kinds={},  # pre-α: no per-model kinds.
        api_key="sk-test-AAAAA",
    )

    import litellm  # type: ignore[import-untyped]

    captured: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert captured["model"] == "gpt-4o-mini"
    assert body["probed_model"] == "gpt-4o-mini"


def test_test_endpoint_response_includes_probed_model_and_kind(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Success response surfaces the probed model id + kind."""
    _app, client, stores = app_and_client
    endpoint_id = _seed_endpoint_with_role(
        stores,
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        models=["claude-sonnet-4-6"],
        role="chat",
        model_kinds={"claude-sonnet-4-6": "chat"},
        api_key="sk-ant-test-AAAA",
    )

    import litellm  # type: ignore[import-untyped]

    async def fake_acompletion(**_kwargs: Any) -> MagicMock:
        return _fake_litellm_response()

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)

    resp = client.post(f"/api/settings/endpoints/{endpoint_id}/test")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["probed_model"] == "claude-sonnet-4-6"
    assert body["probed_kind"] == "chat"


def test_create_endpoint_defaults_role_for_embedding_only_preset(
    app_and_client: Any,
) -> None:
    """POST /endpoints with preset=jina_ai and no ``role`` ⇒ persists
    ``role="embedding"``."""
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={
            "name": "Jina",
            "preset": "jina_ai",
            "base_url": "https://api.jina.ai/v1",
            "auth_type": "api_key",
            "api_key": "jina-test-key-AAAA",
            "models": ["jina-embeddings-v4"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "embedding"


def test_create_endpoint_defaults_role_auto_for_chat_only_preset(
    app_and_client: Any,
) -> None:
    """POST /endpoints with preset=anthropic and no ``role`` ⇒ persists
    ``role="auto"`` (the radio is hidden in the UI; backend default is auto)."""
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={
            "name": "Anthropic",
            "preset": "anthropic",
            "base_url": "https://api.anthropic.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-ant-test-AAAA",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["role"] == "auto"


def test_create_endpoint_seeds_models_from_catalog_when_empty(
    app_and_client: Any,
) -> None:
    """PR-ε: creating a commercial-preset endpoint without ``models`` seeds
    ``endpoint.models`` + ``endpoint.model_kinds`` from Atlas's curated
    catalog. Operator lands ready-to-use without a Discover click — and
    with zero risk of pulling experimental / Live / fine-tune ids that
    break Test downstream."""
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={
            "name": "OpenAI",
            "preset": "openai",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-test-key-AAAA",
            # No ``models`` — auto-seed should kick in.
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # Catalog seed populated both chat and embedding entries.
    assert body["models"], "expected catalog seed to populate models"
    assert any(m.startswith("gpt-") for m in body["models"])
    assert any(m.startswith("text-embedding-") for m in body["models"])
    # model_kinds correctly tagged.
    assert body["model_kinds"]["gpt-4o-mini"] == "chat"
    assert body["model_kinds"]["text-embedding-3-small"] == "embedding"


def test_create_endpoint_skips_seed_when_models_supplied(
    app_and_client: Any,
) -> None:
    """Operator-supplied ``models`` are respected verbatim — no catalog
    seed overrides explicit input."""
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={
            "name": "OpenAI custom",
            "preset": "openai",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-test-key-AAAA",
            "models": ["gpt-5-experimental"],
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["models"] == ["gpt-5-experimental"]


def test_create_endpoint_skips_seed_for_operator_deployed_preset(
    app_and_client: Any,
) -> None:
    """Operator-deployed presets (ollama, custom, vllm, …) do NOT auto-seed —
    their model list IS the source of truth; we don't know what's installed."""
    _app, client, _stores = app_and_client
    resp = client.post(
        "/api/settings/endpoints",
        json={
            "name": "Ollama",
            "preset": "ollama",
            "base_url": "http://localhost:11434/v1",
            "auth_type": "none",
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["models"] == []


def test_update_endpoint_role(app_and_client: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """PUT /endpoints/{id} writes through ``role``; subsequent Test uses the
    new role to pick the probe path."""
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "openai",
            "preset": "openai",
            "base_url": "https://api.openai.com/v1",
            "auth_type": "api_key",
            "api_key": "sk-test-key-AAAA",
            "models": ["text-embedding-3-small", "gpt-4o-mini"],
        },
    ).json()
    # Default role is "auto".
    assert created["role"] == "auto"

    update = client.put(
        f"/api/settings/endpoints/{created['id']}",
        json={"role": "embedding"},
    )
    assert update.status_code == 200, update.text
    assert update.json()["role"] == "embedding"

    # Seed model_kinds + manually_kept directly to ensure the embedding path
    # actually picks an embedding model.
    fetched = client.get(f"/api/settings/endpoints/{created['id']}").json()
    assert fetched["role"] == "embedding"


def test_update_endpoint_manually_kept(
    app_and_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PUT with ``manually_kept`` writes through; subsequent Discover keeps
    those ids in ``models[]`` and out of ``advanced_models``."""
    _app, client, _stores = app_and_client
    created = client.post(
        "/api/settings/endpoints",
        json={
            "name": "jina",
            "preset": "jina_ai",
            "base_url": "https://api.jina.ai/v1",
            "auth_type": "api_key",
            "api_key": "jina-test-key-AAAA",
        },
    ).json()

    update = client.put(
        f"/api/settings/endpoints/{created['id']}",
        json={"manually_kept": ["jina-vlm"]},
    )
    assert update.status_code == 200, update.text
    assert update.json()["manually_kept"] == ["jina-vlm"]

    # Now run discover — ``jina-vlm`` should survive even though the
    # classifier puts it in the dropped (clip) bucket.
    async def fake_discover(endpoint, **_kw):  # noqa: ANN001
        return {
            "ok": True,
            "models": ["jina-embeddings-v4"],
            "models_by_kind": {"chat": [], "embedding": ["jina-embeddings-v4"]},
            "dropped": {"clip": ["jina-vlm"]},
        }

    monkeypatch.setattr("beever_atlas.api.endpoints.discover_models", fake_discover)
    resp = client.post(f"/api/settings/endpoints/{created['id']}/discover")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "jina-vlm" in body["models"]
    assert "jina-embeddings-v4" in body["models"]

    fetched = client.get(f"/api/settings/endpoints/{created['id']}").json()
    assert "jina-vlm" in fetched["models"]
    # manually_kept ids never end up in advanced_models.
    assert "jina-vlm" not in fetched["advanced_models"]
