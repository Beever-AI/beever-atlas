"""PR-B: Endpoint catalog — encryption + CRUD + two-same-preset coexistence.

Exercises :class:`beever_atlas.llm.endpoints.EndpointStore` against an
in-memory async mock of the Mongo collection. The encryption round-trip uses
the real ``CredentialEncryptor`` so a misconfigured master key surfaces here.
"""

from __future__ import annotations

from typing import Any

import pytest

from beever_atlas.llm.endpoints import (
    DEFAULT_PROVIDER_RPM,
    Endpoint,
    EndpointStore,
    decrypt_endpoint_credential,
    encrypt_endpoint_credential,
)


class _UpdateResult:
    """Minimal update_one result with the only fields the store reads."""

    def __init__(self, matched: int, modified: int) -> None:
        self.matched_count = matched
        self.modified_count = modified


class _DeleteResult:
    def __init__(self, deleted: int) -> None:
        self.deleted_count = deleted


class _AsyncCursor:
    """Async iterator over a list — emulates a Mongo cursor."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_AsyncCursor":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _FakeCollection:
    """Minimum-viable async Mongo collection for the store tests."""

    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    def find(
        self, query: dict[str, Any], _projection: dict[str, Any] | None = None
    ) -> _AsyncCursor:
        return _AsyncCursor([d for d in self._docs if self._matches(d, query)])

    async def find_one(
        self, query: dict[str, Any], _projection: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        for d in self._docs:
            if self._matches(d, query):
                return d
        return None

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self._docs.append(dict(doc))

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _UpdateResult:
        for d in self._docs:
            if self._matches(d, query):
                d.update(update.get("$set", {}))
                return _UpdateResult(matched=1, modified=1)
        if upsert:
            new = dict(update.get("$set", {}))
            new.update(query)
            self._docs.append(new)
        return _UpdateResult(matched=0, modified=0)

    async def delete_one(self, query: dict[str, Any]) -> _DeleteResult:
        for d in list(self._docs):
            if self._matches(d, query):
                self._docs.remove(d)
                return _DeleteResult(deleted=1)
        return _DeleteResult(deleted=0)

    @staticmethod
    def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
        if not query:
            return True
        if "$or" in query:
            return any(_FakeCollection._matches(doc, q) for q in query["$or"])
        return all(doc.get(k) == v for k, v in query.items())


class _FakeMongoStore:
    def __init__(self) -> None:
        self.db = {"endpoints": _FakeCollection()}


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> EndpointStore:
    # Configure a master key for the encryption helpers.
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "0" * 64)  # 32 bytes hex
    return EndpointStore(_FakeMongoStore())


@pytest.mark.asyncio
async def test_create_persists_encrypted_credential(store: EndpointStore) -> None:
    """Plaintext key never appears in the persisted document."""
    plaintext = "sk-ant-real-secret-XYZ"
    ep = await store.create(
        name="Anthropic prod",
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        auth_type="api_key",
        plaintext_credential=plaintext,
    )

    assert ep.id  # uuid generated
    assert ep.name == "Anthropic prod"
    assert ep.encrypted_key is not None
    # The plaintext must NOT appear anywhere in the persisted document.
    persisted = await store.get(ep.id)
    assert persisted is not None
    serialised = repr(persisted.to_document())
    assert plaintext not in serialised


@pytest.mark.asyncio
async def test_round_trip_decryption(store: EndpointStore) -> None:
    plaintext = "AIzaSy-fake-google-key"
    ep = await store.create(
        name="Google AI",
        preset="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
        auth_type="api_key",
        plaintext_credential=plaintext,
    )
    assert ep.encrypted_key is not None
    decrypted = decrypt_endpoint_credential(ep.encrypted_key)
    assert decrypted == plaintext


@pytest.mark.asyncio
async def test_two_endpoints_same_preset_coexist(store: EndpointStore) -> None:
    """OpenAI prod + OpenAI staging — two distinct UUIDs, two distinct buckets."""
    prod = await store.create(
        name="OpenAI prod",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-prod-key",
    )
    staging = await store.create(
        name="OpenAI staging",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-staging-key",
    )
    assert prod.id != staging.id
    listed = await store.list()
    assert len(listed) == 2
    assert {e.name for e in listed} == {"OpenAI prod", "OpenAI staging"}


@pytest.mark.asyncio
async def test_default_rpm_per_preset(store: EndpointStore) -> None:
    """Default RPM matches DEFAULT_PROVIDER_RPM when operator omits the field."""
    ep = await store.create(
        name="g",
        preset="groq",
        base_url="https://api.groq.com/openai/v1",
        auth_type="api_key",
        plaintext_credential="gsk-fake",
    )
    assert ep.rpm == DEFAULT_PROVIDER_RPM["groq"]


@pytest.mark.asyncio
async def test_update_credential(store: EndpointStore) -> None:
    """PUT-style update re-encrypts the new key; the old plaintext is unrecoverable."""
    ep = await store.create(
        name="O",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-old",
    )
    updated = await store.update(ep.id, plaintext_credential="sk-new")
    assert updated is not None
    assert updated.encrypted_key is not None
    decrypted = decrypt_endpoint_credential(updated.encrypted_key)
    assert decrypted == "sk-new"


@pytest.mark.asyncio
async def test_update_preserves_credential_when_unspecified(store: EndpointStore) -> None:
    """Updating other fields without passing ``plaintext_credential`` keeps the old envelope."""
    ep = await store.create(
        name="A",
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-ant-original",
    )
    original_envelope = ep.encrypted_key

    updated = await store.update(ep.id, name="A renamed", rpm=42)
    assert updated is not None
    assert updated.name == "A renamed"
    assert updated.rpm == 42
    # Envelope is unchanged — same iv/tag triple.
    assert updated.encrypted_key == original_envelope


@pytest.mark.asyncio
async def test_clear_credential_via_none(store: EndpointStore) -> None:
    """Passing ``plaintext_credential=None`` clears the envelope."""
    ep = await store.create(
        name="O",
        preset="ollama",
        base_url="http://localhost:11434/v1",
        auth_type="none",
        plaintext_credential=None,
    )
    assert ep.encrypted_key is None
    # Round-trip: clear an existing endpoint's credential.
    ep2 = await store.create(
        name="O2",
        preset="openai",
        base_url="https://x",
        auth_type="api_key",
        plaintext_credential="sk-temporary",
    )
    cleared = await store.update(ep2.id, plaintext_credential=None, auth_type="none")
    assert cleared is not None
    assert cleared.encrypted_key is None


@pytest.mark.asyncio
async def test_delete(store: EndpointStore) -> None:
    ep = await store.create(
        name="X",
        preset="openai",
        base_url="https://x",
        auth_type="api_key",
        plaintext_credential="sk-x",
    )
    assert await store.delete(ep.id) is True
    assert await store.get(ep.id) is None
    assert await store.delete(ep.id) is False  # idempotent miss


@pytest.mark.asyncio
async def test_record_test_result(store: EndpointStore) -> None:
    ep = await store.create(
        name="X",
        preset="openai",
        base_url="https://x",
        auth_type="api_key",
        plaintext_credential="sk-x",
    )
    await store.record_test_result(ep.id, ok=False, error="invalid_api_key")
    updated = await store.get(ep.id)
    assert updated is not None
    assert updated.last_test_ok is False
    assert updated.last_test_error == "invalid_api_key"
    assert updated.last_test_at  # ISO timestamp set


def test_encrypt_decrypt_string_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """The standalone helpers handle both string + dict plaintext shapes."""
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "0" * 64)
    env = encrypt_endpoint_credential("sk-test-secret")
    assert env["ciphertext_b64"]
    assert env["iv_b64"]
    assert env["tag_b64"]
    assert "sk-test-secret" not in env["ciphertext_b64"]  # base64 of ciphertext
    decrypted = decrypt_endpoint_credential(env)
    assert decrypted == "sk-test-secret"


def test_encrypt_decrypt_dict_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "0" * 64)
    payload = {
        "access_key_id": "AKIA-FAKE",
        "secret_access_key": "wJalrXUt-FAKE",
        "region": "us-east-1",
    }
    env = encrypt_endpoint_credential(payload)
    decrypted = decrypt_endpoint_credential(env)
    assert decrypted == payload


def test_decrypt_empty_envelope_returns_none() -> None:
    """Malformed/missing envelope returns None instead of crashing."""
    assert decrypt_endpoint_credential({}) is None  # type: ignore[arg-type]


def test_endpoint_from_document_tolerates_missing_fields() -> None:
    ep = Endpoint.from_document({"id": "abc", "preset": "openai"})
    assert ep.id == "abc"
    assert ep.preset == "openai"
    assert ep.name == ""
    assert ep.models == []
    assert ep.rpm == DEFAULT_PROVIDER_RPM["openai"]


def test_endpoint_from_document_defaults_pr_alpha_fields() -> None:
    """Old documents pre-dating PR-α lack model_kinds / advanced_models /
    manually_kept / role — hydrate them as empty containers + role=auto."""
    ep = Endpoint.from_document({"id": "abc", "preset": "openai", "models": ["gpt-4o"]})
    assert ep.model_kinds == {}
    assert ep.advanced_models == []
    assert ep.manually_kept == []
    assert ep.role == "auto"


def test_endpoint_to_document_round_trips_pr_alpha_fields() -> None:
    """A populated PR-α endpoint serialises cleanly and re-hydrates intact."""
    ep = Endpoint(
        id="ep-1",
        name="OpenAI prod",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        encrypted_key=None,
        models=["gpt-4o", "text-embedding-3-small"],
        rpm=500,
        model_kinds={"gpt-4o": "chat", "text-embedding-3-small": "embedding"},
        advanced_models=["whisper-1", "dall-e-3"],
        manually_kept=["custom-model-id"],
        role="chat",
    )
    doc = ep.to_document()
    assert doc["model_kinds"] == {"gpt-4o": "chat", "text-embedding-3-small": "embedding"}
    assert doc["advanced_models"] == ["whisper-1", "dall-e-3"]
    assert doc["manually_kept"] == ["custom-model-id"]
    assert doc["role"] == "chat"

    rehydrated = Endpoint.from_document(doc)
    assert rehydrated.model_kinds == ep.model_kinds
    assert rehydrated.advanced_models == ep.advanced_models
    assert rehydrated.manually_kept == ep.manually_kept
    assert rehydrated.role == ep.role


def test_endpoint_from_document_drops_non_chat_embedding_kinds() -> None:
    """A malformed document with stray 'reranker' values in ``model_kinds``
    is sanitised — only ``chat`` / ``embedding`` survive the hydration."""
    ep = Endpoint.from_document(
        {
            "id": "abc",
            "preset": "openai",
            "model_kinds": {"gpt-4o": "chat", "weird": "reranker", "x": "image_gen"},
        }
    )
    assert ep.model_kinds == {"gpt-4o": "chat"}
