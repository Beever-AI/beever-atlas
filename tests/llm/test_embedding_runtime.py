"""Tests for the live runtime config + migration gate (Layers 3 + 4)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from beever_atlas.infra.config import Settings
from beever_atlas.llm import embedding_runtime as rt


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """Clear embedding-related env + module caches between tests.

    ``get_settings()`` is ``@lru_cache``'d so the env-strip below is a
    no-op unless we also clear that cache — otherwise Settings stays
    pinned to whatever the first call (during conftest fixture setup)
    captured from a local ``.env``. Without clearing it, a developer
    machine with ``EMBEDDING_PROVIDER=gemini`` in ``.env`` will see
    these tests assert ``provider == "jina_ai"`` and fail because the
    cached Settings still reflects the env file rather than the
    monkeypatch-stripped state.
    """
    import os
    import tempfile

    from beever_atlas.infra.config import get_settings

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
    # Pydantic Settings ALSO reads ``.env`` from cwd — ``delenv`` only
    # affects ``os.environ``. A dev machine whose ``.env`` contains
    # ``EMBEDDING_PROVIDER=gemini`` would leak through Settings even
    # after ``delenv``. ``chdir`` into a tempdir so Settings can't find
    # ``.env`` from cwd, ensuring tests rely only on the Field defaults
    # in the ``Settings`` class definition.
    _tmp = tempfile.TemporaryDirectory()
    _prev_cwd = os.getcwd()
    os.chdir(_tmp.name)
    get_settings.cache_clear()
    rt.bust_embedding_settings_cache()
    yield
    os.chdir(_prev_cwd)
    _tmp.cleanup()
    get_settings.cache_clear()
    rt.bust_embedding_settings_cache()


def _mock_stores(*, db_doc=None, secret_key=None, fact_count=0, meta=None):
    settings_collection = AsyncMock()
    settings_collection.find_one = AsyncMock(return_value=db_doc)

    db = {"embedding_settings": settings_collection}

    mongodb = SimpleNamespace(
        get_embedding_meta=AsyncMock(return_value=meta),
        get_embedding_secret=AsyncMock(return_value=None),
        db=db,
    )
    weaviate = SimpleNamespace(count_facts=AsyncMock(return_value=fact_count))
    return SimpleNamespace(mongodb=mongodb, weaviate=weaviate)


# ─── Effective config ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_default_returns_env_only(monkeypatch):
    """No DB doc → effective config matches env-derived Settings."""
    stores = _mock_stores()
    monkeypatch.setattr(
        "beever_atlas.stores.get_stores",
        lambda: stores,
    )
    eff = await rt.get_effective_embedding_settings()
    assert eff.provider == "jina_ai"
    assert eff.model == "jina-embeddings-v4"
    assert eff.dimensions == 2048


@pytest.mark.asyncio
async def test_db_overlay_overrides_env(monkeypatch):
    """A persisted ``embedding_settings`` doc shadows env values."""
    stores = _mock_stores(
        db_doc={
            "_id": "embedding_settings",
            "provider": "openai",
            "model": "text-embedding-3-small",
            "dimensions": 1536,
        }
    )
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    eff = await rt.get_effective_embedding_settings()
    assert eff.provider == "openai"
    assert eff.model == "text-embedding-3-small"
    assert eff.dimensions == 1536


@pytest.mark.asyncio
async def test_cache_returns_within_ttl(monkeypatch):
    """Two calls within 5s hit the cache (find_one only called once)."""
    stores = _mock_stores()
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    await rt.get_effective_embedding_settings()
    await rt.get_effective_embedding_settings()
    # find_one should fire exactly once across two reads.
    assert stores.mongodb.db["embedding_settings"].find_one.await_count == 1


@pytest.mark.asyncio
async def test_bust_forces_refresh(monkeypatch):
    """Explicit cache-bust → next call re-queries."""
    stores = _mock_stores()
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    await rt.get_effective_embedding_settings()
    rt.bust_embedding_settings_cache()
    await rt.get_effective_embedding_settings()
    assert stores.mongodb.db["embedding_settings"].find_one.await_count == 2


@pytest.mark.asyncio
async def test_cache_ttl_expires(monkeypatch):
    """After ``_CACHE_TTL_SECONDS`` the next call refetches."""
    stores = _mock_stores()
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    await rt.get_effective_embedding_settings()

    # Fake 6 seconds passing.
    rt._cached_config_ts -= 6.0  # type: ignore[attr-defined]
    await rt.get_effective_embedding_settings()
    assert stores.mongodb.db["embedding_settings"].find_one.await_count == 2


# ─── Migration gate ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_in_progress_true_on_dim_mismatch_with_facts(monkeypatch):
    """Persisted dim != effective dim AND Weaviate has rows → in_progress."""
    stores = _mock_stores(
        meta={"dimensions": 2048, "provider": "jina_ai", "model": "jina-embeddings-v4"},
        fact_count=12_000,
        db_doc={
            "_id": "embedding_settings",
            "provider": "openai",
            "model": "text-embedding-3-large",
            "dimensions": 3072,
        },
    )
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    assert await rt.is_migration_in_progress() is True


@pytest.mark.asyncio
async def test_migration_false_on_empty_weaviate(monkeypatch):
    """Dim mismatch but no stored facts → fresh-config flip, not migration."""
    stores = _mock_stores(
        meta={"dimensions": 2048, "provider": "jina_ai", "model": "jina-embeddings-v4"},
        fact_count=0,
        db_doc={"_id": "embedding_settings", "dimensions": 768},
    )
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    assert await rt.is_migration_in_progress() is False


@pytest.mark.asyncio
async def test_migration_false_when_dims_match(monkeypatch):
    """Persisted dim == effective dim → no migration."""
    stores = _mock_stores(
        meta={"dimensions": 2048, "provider": "jina_ai", "model": "jina-embeddings-v4"},
        fact_count=12_000,
    )
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    assert await rt.is_migration_in_progress() is False


@pytest.mark.asyncio
async def test_migration_fail_open_on_mongo_error(monkeypatch):
    """MongoDB error during gate check → return False (fail-open)."""
    bad_mongo = SimpleNamespace(
        get_embedding_meta=AsyncMock(side_effect=ConnectionError("mongo down")),
    )
    bad_stores = SimpleNamespace(mongodb=bad_mongo, weaviate=SimpleNamespace())
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: bad_stores)
    assert await rt.is_migration_in_progress() is False


# ─── Context-var bypass ───────────────────────────────────────────────────


def test_context_var_default_false():
    assert rt.in_migration_context() is False


def test_context_var_set_and_reset():
    token = rt.set_migration_context(True)
    try:
        assert rt.in_migration_context() is True
    finally:
        rt.reset_migration_context(token)
    assert rt.in_migration_context() is False


# ─── Embed_texts integration with the gate ────────────────────────────────


@pytest.mark.asyncio
async def test_embed_texts_raises_during_migration(monkeypatch):
    """``embed_texts`` raises ``EmbeddingMigrationInProgress`` when the gate
    fires (no settings= override + no contextvar set)."""
    from beever_atlas.llm import embeddings as emb

    stores = _mock_stores(
        meta={"dimensions": 2048},
        fact_count=12_000,
        db_doc={"_id": "embedding_settings", "dimensions": 3072},
    )
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    rt.bust_embedding_settings_cache()

    with pytest.raises(rt.EmbeddingMigrationInProgress):
        await emb.embed_texts(["test"])


@pytest.mark.asyncio
async def test_embed_texts_bypasses_gate_with_contextvar(monkeypatch):
    """Setting the migration contextvar lets the migration job's own embed
    calls flow through even though the gate would otherwise fire."""
    from beever_atlas.llm import embeddings as emb

    stores = _mock_stores(
        meta={"dimensions": 2048},
        fact_count=12_000,
        db_doc={"_id": "embedding_settings", "dimensions": 3072},
    )
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    rt.bust_embedding_settings_cache()

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        return [[0.0] * 3072 for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)

    token = rt.set_migration_context(True)
    try:
        out = await emb.embed_texts(["test"])
    finally:
        rt.reset_migration_context(token)

    assert len(out) == 1


@pytest.mark.asyncio
async def test_embed_texts_settings_kwarg_bypasses_gate(monkeypatch):
    """Explicit ``settings=`` kwarg (test/probe path) bypasses the gate."""
    from beever_atlas.llm import embeddings as emb

    stores = _mock_stores(
        meta={"dimensions": 2048},
        fact_count=12_000,
        db_doc={"_id": "embedding_settings", "dimensions": 3072},
    )
    monkeypatch.setattr("beever_atlas.stores.get_stores", lambda: stores)
    rt.bust_embedding_settings_cache()

    async def fake_call(*, model, chunk, extra_kwargs, **kwargs):
        return [[0.0] * 4 for _ in chunk]

    monkeypatch.setattr(emb, "_aembedding_call", fake_call)

    test_settings = Settings(
        embedding_provider="openai",
        embedding_model="text-embedding-3-small",
        embedding_dimensions=1536,
    )
    out = await emb.embed_texts(["test"], settings=test_settings)
    assert len(out) == 1
