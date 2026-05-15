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
    """Strip embedding-related env vars + DB overrides to keep tests
    deterministic.

    ``probe_and_validate`` now resolves effective settings through
    ``resolve_effective_settings`` which loads DB overrides from
    MongoDB. In a dev/CI environment the local Mongo may carry a stale
    ``embedding_settings`` doc from another test or an interactive
    session, which would silently override ``cfg.embedding_*`` and
    break this test's pre-conditions. Stub the loader to return an
    empty override dict so each test sees only what its ``cfg`` provides.
    """
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

    # Stub DB-override resolution. Tests that need to assert DB-override
    # behaviour should ``monkeypatch.setattr`` this back to a controlled
    # async function in the test body.
    from beever_atlas.llm import embedding_runtime

    async def _no_db_overrides() -> dict[str, Any]:
        return {}

    monkeypatch.setattr(embedding_runtime, "_load_db_overrides", _no_db_overrides)
    embedding_runtime.bust_embedding_settings_cache()


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
async def test_probe_failure_is_soft_fail_app_still_starts(monkeypatch):
    """PR-ζ.5: a probe transport / config failure must NOT crash the app.

    Rationale: the boot guard's job is to prevent data corruption from a
    wrong-dim model writing vectors of the wrong shape into the index.
    A transport failure (404 / auth / network) doesn't produce any
    vectors — so there's no corruption risk. Crashing the whole app on
    a config mistake is hostile: the operator can't reach the UI to fix
    the model they typed wrong. Now we log + persist + continue, and
    the UI's ``embedding-migration/status`` + the front-end "Re-embed
    required" banner surface the failure so the operator can fix it
    interactively.

    The wrong-dim case (probe succeeds but returns vectors of the wrong
    length) still raises — see ``test_probe_dim_disagrees_with_configured_aborts``.
    """
    cfg = _settings()
    set_meta_calls: list[dict[str, object]] = []

    async def fake_set_meta(_stores, **kw):
        set_meta_calls.append(kw)

    stores = _stores(persisted_meta=None, fact_count=0)

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=False, dim=None, latency_ms=0, error="auth failed")

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    monkeypatch.setattr(health_mod, "_safe_set_meta", fake_set_meta)

    h = await probe_and_validate(cfg, stores)

    # App didn't crash — health bubbled up with the underlying error so the
    # API + UI can surface it.
    assert h.ok is False
    assert h.error == "auth failed"
    # PR-η: the failure must NOT touch ``embedding_meta`` — that doc is the
    # source of truth for what's ACTUALLY stored in Weaviate. Writing the
    # configured-but-unvalidated model name into it would corrupt the
    # source: a later ``/state`` lookup would see "persisted == desired"
    # and report ``migration_required=False`` even though no migration ran.
    assert set_meta_calls == []


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


@pytest.mark.asyncio
async def test_probe_uses_db_override_when_env_disagrees(monkeypatch):
    """Regression: a user-saved UI override in ``embedding_settings`` must
    win over the env-derived ``Settings`` at boot. Previously the boot
    probe used env-only, so switching Gemini→Jina via the UI required
    also editing ``.env`` for boot to pass.
    """
    # Env says Gemini@3072, DB says Jina@2048. Probe must run as Jina.
    cfg = _settings(
        embedding_provider="gemini",
        embedding_model="gemini-embedding-001",
        embedding_dimensions=3072,
    )
    stores = _stores(persisted_meta=None, fact_count=0)

    from beever_atlas.llm import embedding_runtime

    async def db_overrides() -> dict[str, Any]:
        return {
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
            "api_key": "jina-secret",
        }

    monkeypatch.setattr(embedding_runtime, "_load_db_overrides", db_overrides)
    embedding_runtime.bust_embedding_settings_cache()

    captured: dict[str, Any] = {}

    async def fake_probe(s):
        captured["provider"] = s.embedding_provider
        captured["model"] = s.embedding_model
        captured["dimensions"] = s.embedding_dimensions
        captured["api_key"] = s.embedding_api_key
        return EmbeddingHealth(ok=True, dim=2048, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)

    h = await probe_and_validate(cfg, stores)
    assert h.ok

    # The probe was called with effective (DB-overridden) values.
    assert captured["provider"] == "jina_ai"
    assert captured["model"] == "jina-embeddings-v4"
    assert captured["dimensions"] == 2048
    assert captured["api_key"] == "jina-secret"

    # ``embedding_meta`` records the effective config too, not the env baseline.
    args = stores.mongodb.set_embedding_meta.await_args.kwargs
    assert args["provider"] == "jina_ai"
    assert args["model"] == "jina-embeddings-v4"
    assert args["dimensions"] == 2048


@pytest.mark.asyncio
async def test_ui_initiated_migration_downgrades_persisted_mismatch_to_warn(monkeypatch, caplog):
    """When env baseline differs from effective settings (i.e. a UI-saved
    override is active), the persisted-vs-effective check downgrades to
    a WARN instead of aborting boot. The runtime migration gate handles
    the actual corruption risk.

    Also asserts that ``embedding_meta`` is NOT updated in this branch —
    the persisted dim stays pinned until the migration job completes.
    """
    # Env says Gemini@3072; DB override says Jina@2048 — same scenario the
    # user is actually in.
    cfg = _settings(
        embedding_provider="gemini",
        embedding_model="gemini-embedding-001",
        embedding_dimensions=3072,
    )
    stores = _stores(
        persisted_meta={
            "provider": "gemini",
            "model": "gemini-embedding-001",
            "dimensions": 3072,
        },
        fact_count=696,
    )

    from beever_atlas.llm import embedding_runtime

    async def db_overrides() -> dict[str, Any]:
        return {
            "provider": "jina_ai",
            "model": "jina-embeddings-v4",
            "dimensions": 2048,
        }

    monkeypatch.setattr(embedding_runtime, "_load_db_overrides", db_overrides)
    embedding_runtime.bust_embedding_settings_cache()

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=2048, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)

    import logging

    monkeypatch.setattr(logging.getLogger("beever_atlas"), "propagate", True)

    with caplog.at_level("WARNING", logger="beever_atlas.llm.embedding_health"):
        h = await probe_and_validate(cfg, stores)

    assert h.ok  # Boot proceeds.
    assert any("UI-initiated migration pending" in rec.message for rec in caplog.records), (
        "Expected the UI-initiated migration WARN to be logged"
    )

    # CRITICAL: embedding_meta MUST NOT be updated in this branch — the
    # runtime gate relies on persisted dim != effective dim to keep
    # blocking queries until the re-embed completes.
    stores.mongodb.set_embedding_meta.assert_not_called()


@pytest.mark.asyncio
async def test_env_only_dim_change_with_facts_still_aborts(monkeypatch):
    """If the dim change came purely from env (no DB override, no UI
    confirm step), keep the hard abort — operators can still bypass via
    ``EMBEDDING_DIM_GUARD=false`` or by going through the UI flow.
    """
    # No DB override — effective == env. The user changed env directly.
    cfg = _settings(
        embedding_provider="jina_ai",
        embedding_model="jina-embeddings-v4",
        embedding_dimensions=2048,
    )
    stores = _stores(
        persisted_meta={
            "provider": "gemini",
            "model": "gemini-embedding-001",
            "dimensions": 3072,
        },
        fact_count=696,
    )

    async def fake_probe(_settings):
        return EmbeddingHealth(ok=True, dim=2048, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)

    # ``_load_db_overrides`` already stubbed to {} by the autouse fixture,
    # so effective == cfg == env. Persisted differs → hard abort.
    with pytest.raises(EmbeddingDimensionMismatch):
        await probe_and_validate(cfg, stores)


@pytest.mark.asyncio
async def test_probe_falls_back_to_env_when_db_override_resolution_fails(monkeypatch):
    """If ``resolve_effective_settings`` cannot reach Mongo, the boot probe
    must still run using env Settings rather than aborting startup.
    """
    cfg = _settings()  # env Jina@2048
    stores = _stores(persisted_meta=None, fact_count=0)

    from beever_atlas.llm import embedding_runtime

    async def boom() -> dict[str, Any]:
        raise RuntimeError("mongo down")

    monkeypatch.setattr(embedding_runtime, "_load_db_overrides", boom)
    embedding_runtime.bust_embedding_settings_cache()

    captured: dict[str, Any] = {}

    async def fake_probe(s):
        captured["dimensions"] = s.embedding_dimensions
        return EmbeddingHealth(ok=True, dim=2048, latency_ms=30)

    monkeypatch.setattr(health_mod, "_run_probe", fake_probe)
    h = await probe_and_validate(cfg, stores)
    assert h.ok
    # Probe used env baseline because DB-override resolution failed.
    assert captured["dimensions"] == 2048
