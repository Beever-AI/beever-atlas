"""PR-B: agent_credentials — process-local runtime credential cache."""

from __future__ import annotations

from typing import Any

import pytest

from beever_atlas.llm.agent_credentials import (
    clear_all_runtime_credentials,
    get_runtime_credential,
    hydrate_runtime_credentials,
    runtime_credential_count,
    set_runtime_credential,
)


@pytest.fixture(autouse=True)
def _reset_runtime_between_tests() -> None:
    clear_all_runtime_credentials()


def test_set_and_get_runtime_credential() -> None:
    set_runtime_credential("ep-1", "sk-secret")
    assert get_runtime_credential("ep-1") == "sk-secret"
    assert runtime_credential_count() == 1


def test_set_none_clears_entry() -> None:
    set_runtime_credential("ep-1", "sk-secret")
    set_runtime_credential("ep-1", None)
    assert get_runtime_credential("ep-1") is None
    assert runtime_credential_count() == 0


def test_get_missing_returns_none() -> None:
    assert get_runtime_credential("never-set") is None


def test_clear_all_empties_cache() -> None:
    set_runtime_credential("a", "1")
    set_runtime_credential("b", "2")
    clear_all_runtime_credentials()
    assert runtime_credential_count() == 0


def test_dict_credential_round_trip() -> None:
    """AWS IAM / Vertex SA paths cache a dict blob, not a string."""
    iam = {"access_key_id": "AKIA-X", "secret_access_key": "wJal-X", "region": "us-east-1"}
    set_runtime_credential("ep-bedrock", iam)
    assert get_runtime_credential("ep-bedrock") == iam


@pytest.mark.asyncio
async def test_hydrate_loads_every_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """Boot hydration walks ``EndpointStore.list()`` and decrypts each entry."""
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "0" * 64)
    from beever_atlas.llm.endpoints import EndpointStore

    # Build a minimal stores stub with two encrypted endpoints + one no-auth.
    class _StoresStub:
        class _MongoStub:
            db: dict[str, Any]

            def __init__(self) -> None:
                self.db = {"endpoints": _FakeCollection()}

        mongodb = _MongoStub()

    stores = _StoresStub()
    store = EndpointStore(stores.mongodb)

    await store.create(
        name="A",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-A",
    )
    await store.create(
        name="B",
        preset="anthropic",
        base_url="https://api.anthropic.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-B",
    )
    await store.create(
        name="local",
        preset="ollama",
        base_url="http://localhost:11434/v1",
        auth_type="none",
        plaintext_credential=None,
    )

    loaded = await hydrate_runtime_credentials(stores)
    assert loaded == 2  # the no-auth Ollama endpoint is skipped


@pytest.mark.asyncio
async def test_hydrate_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated hydrations don't grow the cache."""
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "0" * 64)
    from beever_atlas.llm.endpoints import EndpointStore

    class _StoresStub:
        class _MongoStub:
            def __init__(self) -> None:
                self.db = {"endpoints": _FakeCollection()}

        mongodb = _MongoStub()

    stores = _StoresStub()
    store = EndpointStore(stores.mongodb)
    await store.create(
        name="A",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-A",
    )
    await hydrate_runtime_credentials(stores)
    await hydrate_runtime_credentials(stores)
    assert runtime_credential_count() == 1


@pytest.mark.asyncio
async def test_hydrate_skips_corrupt_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad envelope on one Endpoint must not block boot — log + skip."""
    monkeypatch.setenv("CREDENTIAL_MASTER_KEY", "0" * 64)
    from beever_atlas.llm.endpoints import EndpointStore

    class _StoresStub:
        class _MongoStub:
            def __init__(self) -> None:
                self.db = {"endpoints": _FakeCollection()}

        mongodb = _MongoStub()

    stores = _StoresStub()
    store = EndpointStore(stores.mongodb)

    # Good endpoint
    await store.create(
        name="A",
        preset="openai",
        base_url="https://api.openai.com/v1",
        auth_type="api_key",
        plaintext_credential="sk-A",
    )
    # Inject a bad envelope directly (envelope's tag fails to decrypt)
    bad_doc = {
        "id": "bad-endpoint",
        "name": "bad",
        "preset": "openai",
        "base_url": "https://x",
        "auth_type": "api_key",
        "encrypted_key": {
            "ciphertext_b64": "AA==",
            "iv_b64": "AAAAAAAAAAAA",
            "tag_b64": "AAAAAAAAAAAAAAAAAAAAAA==",
        },
        "models": [],
        "rpm": 500,
    }
    stores.mongodb.db["endpoints"]._docs.append(bad_doc)  # type: ignore[attr-defined]

    loaded = await hydrate_runtime_credentials(stores)
    # Only the good endpoint hydrated.
    assert loaded == 1
    # Boot survived the bad envelope.


# ────────────────────────────────────────────────────────────────────────
# Reuse the same _FakeCollection shape from the endpoint-store test so the
# hydration path exercises the real EndpointStore.list() call. Inlined to
# avoid cross-test imports.
# ────────────────────────────────────────────────────────────────────────


class _AsyncCursor:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self) -> "_AsyncCursor":
        return self

    async def __anext__(self) -> dict[str, Any]:
        if not self._items:
            raise StopAsyncIteration
        return self._items.pop(0)


class _UpdateResult:
    def __init__(self, matched: int) -> None:
        self.matched_count = matched
        self.modified_count = matched


class _DeleteResult:
    def __init__(self, deleted: int) -> None:
        self.deleted_count = deleted


class _FakeCollection:
    def __init__(self) -> None:
        self._docs: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any], _projection: Any = None) -> _AsyncCursor:
        return _AsyncCursor(list(self._docs))

    async def find_one(self, query: dict[str, Any], _projection: Any = None) -> Any:
        for d in self._docs:
            if all(d.get(k) == v for k, v in query.items()):
                return d
        return None

    async def insert_one(self, doc: dict[str, Any]) -> None:
        self._docs.append(dict(doc))

    async def update_one(
        self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False
    ) -> _UpdateResult:
        for d in self._docs:
            if all(d.get(k) == v for k, v in query.items()):
                d.update(update.get("$set", {}))
                return _UpdateResult(matched=1)
        if upsert:
            new = dict(update.get("$set", {}))
            new.update(query)
            self._docs.append(new)
        return _UpdateResult(matched=0)

    async def delete_one(self, query: dict[str, Any]) -> _DeleteResult:
        for d in list(self._docs):
            if all(d.get(k) == v for k, v in query.items()):
                self._docs.remove(d)
                return _DeleteResult(deleted=1)
        return _DeleteResult(deleted=0)
