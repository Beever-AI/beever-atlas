"""PR-C: boot-time embedding probe + dimension guard.

Covers every Requirement scenario in
``specs/embedding-dimension-guard/spec.md``:

  * Successful probe records meta + allows boot.
  * Probe dim disagreement vs configured dim → fatal.
  * Mismatch with populated Weaviate aborts.
  * Mismatch on empty Weaviate is allowed.
  * Operator override skips guard.
  * Weaviate unreachable downgrades to warn-and-continue.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from beever_atlas.infra.config import Settings
from beever_atlas.llm import embedding_health as health_mod
from beever_atlas.llm.embedding_health import (
    EmbeddingDimensionMismatch,
    EmbeddingHealth,
    probe_and_validate,
)


# ─── Helpers ───────────────────────────────────────────────────────────────


def _settings(**overrides: Any) -> Settings:
    base = {
        "embedding_provider": "jina_ai",
        "embedding_model": "jina-embeddings-v4",
        "embedding_dimensions": 2048,
        "embedding_dim_guard": True,
    }
    base.update(overrides)
    return Settings(**base)


def _stores(*, persisted_meta: dict | None, fact_count: int) -> Any:
    """Build a minimal mock stores bundle wired with controllable returns."""
    from types import SimpleNamespace

    mongodb = SimpleNamespace(
        get_embedding_meta=AsyncMock(return_value=persisted_meta),
        set_embedding_meta=AsyncMock(),
    )
    weaviate = SimpleNamespace(count_facts=AsyncMock(return_value=fact_count))
    return SimpleNamespace(mongodb=mongodb, weaviate=weaviate)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip embedding-related env vars to keep tests deterministic."""
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


# ─── Tests ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_probe_records_meta(monkeypatch):
    """Happy path: probe matches config → meta upsert + return health."""
    cfg = _settings()
    stores = _stores(persisted_meta=None, fact_count=0)

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=2048, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    h = await probe_and_validate(cfg, stores)

    assert h.ok and h.dim == 2048
    stores.mongodb.set_embedding_meta.assert_awaited_once()
    args = stores.mongodb.set_embedding_meta.await_args.kwargs
    assert args["provider"] == "jina_ai"
    assert args["model"] == "jina-embeddings-v4"
    assert args["dimensions"] == 2048
    assert args["ok"] is True


@pytest.mark.asyncio
async def test_probe_dim_disagrees_with_configured_aborts(monkeypatch):
    """Provider returns 1536-d vector but config says 3072 → fatal."""
    cfg = _settings(embedding_dimensions=3072)
    stores = _stores(persisted_meta=None, fact_count=0)

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=1536, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    with pytest.raises(EmbeddingDimensionMismatch) as excinfo:
        await probe_and_validate(cfg, stores)

    msg = str(excinfo.value)
    assert "1536" in msg
    assert "3072" in msg


@pytest.mark.asyncio
async def test_dim_mismatch_with_populated_weaviate_aborts(monkeypatch):
    """Configured 3072 vs persisted 2048 + 12,847 facts → fatal."""
    cfg = _settings(
        embedding_dimensions=3072,
        embedding_model="text-embedding-3-large",
        embedding_provider="openai",
    )
    stores = _stores(
        persisted_meta={
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
        },
        fact_count=12847,
    )

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=3072, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    with pytest.raises(EmbeddingDimensionMismatch) as excinfo:
        await probe_and_validate(cfg, stores)

    msg = str(excinfo.value)
    assert "12,847" in msg
    assert "make reembed-all" in msg
    assert "docs/runbooks/embedding-migration.md" in msg


@pytest.mark.asyncio
async def test_dim_mismatch_on_empty_weaviate_is_allowed(monkeypatch):
    """Empty Weaviate → mismatch is OK, meta gets updated."""
    cfg = _settings(
        embedding_dimensions=3072,
        embedding_model="text-embedding-3-large",
        embedding_provider="openai",
    )
    stores = _stores(
        persisted_meta={
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
        },
        fact_count=0,
    )

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=3072, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    h = await probe_and_validate(cfg, stores)

    assert h.ok
    stores.mongodb.set_embedding_meta.assert_awaited()


@pytest.mark.asyncio
async def test_dim_guard_off_warns_and_continues(monkeypatch, caplog):
    """``EMBEDDING_DIM_GUARD=false`` downgrades fatal mismatch to warn."""
    cfg = _settings(
        embedding_dimensions=3072,
        embedding_model="text-embedding-3-large",
        embedding_provider="openai",
        embedding_dim_guard=False,
    )
    stores = _stores(
        persisted_meta={
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
        },
        fact_count=12847,
    )

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=3072, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)

    import logging

    monkeypatch.setattr(logging.getLogger("beever_atlas"), "propagate", True)

    with caplog.at_level("WARNING", logger="beever_atlas.llm.embedding_health"):
        await probe_and_validate(cfg, stores)

    assert any("DIM_GUARD" in rec.message or "12,847" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_weaviate_unreachable_skips_count_check(monkeypatch):
    """Weaviate ``count_facts`` raising → guard logs WARN + boots."""
    cfg = _settings(
        embedding_dimensions=3072,
        embedding_model="text-embedding-3-large",
        embedding_provider="openai",
    )

    from types import SimpleNamespace

    mongodb = SimpleNamespace(
        get_embedding_meta=AsyncMock(
            return_value={
                "provider": "jina_ai",
                "model": "jina-embeddings-v4",
                "dimensions": 2048,
            }
        ),
        set_embedding_meta=AsyncMock(),
    )

    weaviate = SimpleNamespace(count_facts=AsyncMock(side_effect=ConnectionError("weaviate down")))
    stores = SimpleNamespace(mongodb=mongodb, weaviate=weaviate)

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=3072, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    h = await probe_and_validate(cfg, stores)
    assert h.ok


@pytest.mark.asyncio
async def test_probe_failure_aborts(monkeypatch):
    """Probe call itself fails → fatal unless DIM_GUARD off."""
    cfg = _settings()
    stores = _stores(persisted_meta=None, fact_count=0)

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=False, dim=None, latency_ms=0, error="auth failed")

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    with pytest.raises(EmbeddingDimensionMismatch) as excinfo:
        await probe_and_validate(cfg, stores)

    assert "auth failed" in str(excinfo.value)


@pytest.mark.asyncio
async def test_probe_failure_with_guard_off_returns_health(monkeypatch):
    cfg = _settings(embedding_dim_guard=False)
    stores = _stores(persisted_meta=None, fact_count=0)

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=False, dim=None, latency_ms=0, error="auth failed")

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    h = await probe_and_validate(cfg, stores)
    assert not h.ok
    assert h.error == "auth failed"
