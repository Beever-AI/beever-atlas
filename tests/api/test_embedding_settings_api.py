"""PR-E: ``/api/settings/embedding`` endpoint tests.

Covers the ``embedding-settings-ui`` spec scenarios that exercise the API
boundary (UI components are tested separately via Vitest in
``web/src/components/settings/``).

  * GET masks key + reflects source.
  * GET never returns plaintext.
  * PUT encrypts at rest, plaintext absent from DB document.
  * PUT 422 on unknown provider.
  * PUT 409 on dim mismatch without ``confirm_migration``.
  * Test endpoint returns dimensions on success and error string on failure.
  * Migrate endpoint dedups concurrent calls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from beever_atlas.api import embedding_settings as ep
from beever_atlas.llm import embedding_health as health_mod
from beever_atlas.llm.embedding_health import EmbeddingHealth


# ─── Test scaffold ─────────────────────────────────────────────────────────


def _make_stores(
    *,
    secret: dict | None = None,
    settings_doc: dict | None = None,
    fact_count: int = 0,
    meta: dict | None = None,
) -> Any:
    """Build a mock ``StoreClients`` shaped for the embedding API."""
    settings_collection = AsyncMock()
    settings_collection.find_one = AsyncMock(return_value=settings_doc)
    settings_collection.update_one = AsyncMock()

    reembed_state = AsyncMock()
    reembed_state.find_one = AsyncMock(return_value=None)
    reembed_state.update_one = AsyncMock()

    db = {"embedding_settings": settings_collection, "reembed_state": reembed_state}

    mongodb = SimpleNamespace(
        get_embedding_secret=AsyncMock(return_value=secret),
        set_embedding_secret=AsyncMock(),
        clear_embedding_secret=AsyncMock(),
        get_embedding_meta=AsyncMock(return_value=meta),
        set_embedding_meta=AsyncMock(),
        db=db,
    )
    weaviate = SimpleNamespace(count_facts=AsyncMock(return_value=fact_count))
    return SimpleNamespace(mongodb=mongodb, weaviate=weaviate)


@pytest.fixture
def app_and_client(monkeypatch):
    """FastAPI app with the embedding router only — no auth scaffolding."""
    # Reset migration registry between tests.
    ep._active_migration["task"] = None
    ep._active_migration["job_id"] = None
    ep._active_migration["started_at"] = None
    ep._active_migration["error"] = None

    # Strip env vars so ``_resolve_effective_settings`` reports source=default.
    for var in (
        "EMBEDDING_PROVIDER",
        "EMBEDDING_MODEL",
        "EMBEDDING_DIMENSIONS",
        "EMBEDDING_RPM",
        "EMBEDDING_API_BASE",
        "EMBEDDING_API_KEY",
        "EMBEDDING_TASK",
        "JINA_API_URL",
        "JINA_MODEL",
        "JINA_DIMENSIONS",
        "JINA_RPM",
    ):
        monkeypatch.delenv(var, raising=False)
    # Pydantic Settings ALSO reads ``.env`` from disk; ``delenv`` only
    # affects ``os.environ``. ``test_get_returns_default_when_unset`` in
    # particular asserts ``source == "default"`` — i.e. neither env vars
    # NOR a DB doc supplied a value. A dev machine whose ``.env`` contains
    # ``EMBEDDING_PROVIDER=gemini`` would otherwise leak through Settings
    # and break the assertion. ``chdir`` into a tempdir so Settings can't
    # find ``.env`` from cwd.
    import os
    import tempfile

    _tmp = tempfile.TemporaryDirectory()
    _prev_cwd = os.getcwd()
    os.chdir(_tmp.name)

    # CI runners do NOT have a CREDENTIAL_MASTER_KEY in their env. The
    # encrypted-API-key path requires one. Inject a deterministic 32-byte
    # test key (different value than the well-known dev placeholder so the
    # production-mode validator doesn't kick) so the encrypt/decrypt
    # round-trip works inside these tests.
    monkeypatch.setenv(
        "CREDENTIAL_MASTER_KEY",
        "ab" * 32,  # 64 hex chars / 32 bytes
    )
    # Settings is lru-cached; ensure the new master key is picked up.
    from beever_atlas.infra.config import get_settings as _gs

    _gs.cache_clear()

    app = FastAPI()
    app.include_router(ep.router)
    try:
        yield app, TestClient(app)
    finally:
        # Restore cwd + clean up the .env-less tempdir so other tests
        # (and any post-test teardown that resolves relative paths)
        # don't operate from a stale working directory.
        os.chdir(_prev_cwd)
        _tmp.cleanup()


# ─── GET ──────────────────────────────────────────────────────────────────


def test_get_returns_default_when_unset(app_and_client, monkeypatch):
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.get("/api/settings/embedding")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "jina_ai"
    assert body["model"] == "jina-embeddings-v4"
    assert body["dimensions"] == 2048
    assert body["source"] == "default"
    assert body["has_api_key"] is False
    assert body["api_key_masked"] == ""


def test_get_reflects_db_override(app_and_client, monkeypatch):
    app, client = app_and_client
    stores = _make_stores(
        settings_doc={
            "_id": "embedding_settings",
            "provider": "openai",
            "model": "text-embedding-3-large",
            "dimensions": 3072,
        }
    )
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.get("/api/settings/embedding")
    body = resp.json()
    assert body["provider"] == "openai"
    assert body["model"] == "text-embedding-3-large"
    assert body["dimensions"] == 3072
    assert body["source"] == "db"


def test_get_never_includes_plaintext_key(app_and_client, monkeypatch):
    app, client = app_and_client
    # Pre-encrypt a plaintext key and insert into the mock secret store.
    from beever_atlas.infra.crypto import encrypt_credentials

    plaintext = "sk-plaintext-MUST-NEVER-LEAK-1234"
    ciphertext, iv, tag = encrypt_credentials({"api_key": plaintext})
    import base64

    stores = _make_stores(
        secret={
            "ciphertext_b64": base64.b64encode(ciphertext).decode("ascii"),
            "iv_b64": base64.b64encode(iv).decode("ascii"),
            "tag_b64": base64.b64encode(tag).decode("ascii"),
        }
    )
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.get("/api/settings/embedding")
    body_text = resp.text
    assert plaintext not in body_text, "plaintext API key leaked in GET response"
    body = resp.json()
    assert body["has_api_key"] is True
    assert body["api_key_masked"].startswith("sk-p")
    assert body["api_key_masked"].endswith("1234")
    # Mask should not be the full plaintext
    assert body["api_key_masked"] != plaintext


# ─── PUT ──────────────────────────────────────────────────────────────────


def test_put_unknown_provider_422(app_and_client, monkeypatch):
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.put(
        "/api/settings/embedding",
        json={"provider": "fictional", "model": "foo", "dimensions": 2048},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "unsupported_provider"


def test_put_known_model_dim_mismatch_422(app_and_client, monkeypatch):
    """Operator picks ``openai/text-embedding-3-large`` but supplies dim=999."""
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.put(
        "/api/settings/embedding",
        json={
            "provider": "openai",
            "model": "text-embedding-3-large",
            "dimensions": 999,
        },
    )
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "dimension_mismatch_known_model"


def test_put_dim_change_with_populated_weaviate_409(app_and_client, monkeypatch):
    """Persisted dim 2048 + 12k facts + new dim 3072 without confirm → 409."""
    app, client = app_and_client
    stores = _make_stores(
        meta={"provider": "jina_ai", "model": "jina-embeddings-v4", "dimensions": 2048},
        fact_count=12847,
    )
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.put(
        "/api/settings/embedding",
        json={
            "provider": "openai",
            "model": "text-embedding-3-large",
            "dimensions": 3072,
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "dim_mismatch_requires_migration"
    assert detail["current_dim"] == 2048
    assert detail["new_dim"] == 3072
    assert detail["fact_count"] == 12847


def test_put_dim_change_with_confirm_migration_succeeds(app_and_client, monkeypatch):
    """Same scenario as above but ``confirm_migration: true`` → 200."""
    app, client = app_and_client
    stores = _make_stores(
        meta={"provider": "jina_ai", "model": "jina-embeddings-v4", "dimensions": 2048},
        fact_count=12847,
    )
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.put(
        "/api/settings/embedding",
        json={
            "provider": "openai",
            "model": "text-embedding-3-large",
            "dimensions": 3072,
            "confirm_migration": True,
        },
    )
    assert resp.status_code == 200


def test_put_api_key_encrypts_at_rest(app_and_client, monkeypatch):
    """Plaintext key submitted via PUT → encrypted before MongoDB write,
    plaintext does not appear in the persisted ciphertext."""
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    plaintext = "sk-test-plaintext-DO-NOT-LEAK-5678"
    resp = client.put(
        "/api/settings/embedding",
        json={"api_key": plaintext},
    )
    assert resp.status_code == 200
    stores.mongodb.set_embedding_secret.assert_awaited_once()
    call_kwargs = stores.mongodb.set_embedding_secret.await_args.kwargs

    import base64

    ciphertext = base64.b64decode(call_kwargs["ciphertext_b64"])
    # Round-trip decrypt to confirm the key is recoverable but ciphertext
    # alone does NOT match plaintext.
    assert plaintext.encode() not in ciphertext

    # Round-trip via the encryption module to confirm the key is correct.
    from beever_atlas.infra.crypto import decrypt_credentials

    iv = base64.b64decode(call_kwargs["iv_b64"])
    tag = base64.b64decode(call_kwargs["tag_b64"])
    decrypted = decrypt_credentials(ciphertext, iv, tag)
    assert decrypted == {"api_key": plaintext}


# ─── /test ────────────────────────────────────────────────────────────────


def test_test_endpoint_happy(app_and_client, monkeypatch):
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=1536, latency_ms=42)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)

    resp = client.post(
        "/api/settings/embedding/test",
        json={
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimensions": 1536,
            "api_key": "sk-test",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["dimensions"] == 1536
    assert body["latency_ms"] == 42
    # No persistence side effects.
    stores.mongodb.set_embedding_secret.assert_not_awaited()
    stores.mongodb.set_embedding_meta.assert_not_awaited()


def test_test_endpoint_failure_surfaces_error(app_and_client, monkeypatch):
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=False, dim=None, latency_ms=10, error="401 unauthorized")

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)

    resp = client.post(
        "/api/settings/embedding/test",
        json={"api_key": "sk-bad"},
    )
    body = resp.json()
    assert body["ok"] is False
    assert body["error"] == "401 unauthorized"


def test_test_endpoint_unsupported_provider_handled_gracefully(app_and_client, monkeypatch):
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.post(
        "/api/settings/embedding/test",
        json={"provider": "fictional", "model": "abc", "dimensions": 1024},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["ok"] is False
    assert "unsupported provider" in body["error"]


# ─── /migrate ─────────────────────────────────────────────────────────────


def test_migrate_concurrent_returns_existing_job(app_and_client, monkeypatch):
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    # Park a long-running task in the registry so the second call sees it
    # as still running.
    started = asyncio.Event()
    finished = asyncio.Event()

    async def slow_work():
        started.set()
        await finished.wait()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        existing_task = loop.create_task(slow_work())
        ep._active_migration["task"] = existing_task
        ep._active_migration["job_id"] = "EXISTING-JOB"

        resp = client.post("/api/settings/embedding/migrate", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "running_existing"
        assert body["job_id"] == "EXISTING-JOB"
    finally:
        finished.set()
        loop.run_until_complete(existing_task)
        loop.close()


def test_migrate_status_reports_no_run_when_idle(app_and_client, monkeypatch):
    app, client = app_and_client
    stores = _make_stores()
    monkeypatch.setattr(ep, "get_stores", lambda: stores)

    resp = client.get("/api/settings/embedding/migrate/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["running"] is False
    assert body["job_id"] is None
