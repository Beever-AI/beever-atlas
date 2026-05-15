"""Live embedding-config overlay + migration gate (Layers 3 + 4 of the
provider-pluggable embedding feature).

The chat-side ``LLMProvider`` reads ``Settings`` once at boot via
``@lru_cache``-d ``get_settings()``. That means UI-driven changes to
``embedding_settings`` in MongoDB are invisible to the running process —
``embed_texts`` keeps using the env-derived config until restart. This
module fixes that with a 5-second-TTL overlay cache and a cache-bust hook
fired by the PUT handler.

It also exposes the migration-mode gate. While a re-embed migration is
in flight, query and ingestion paths must NOT call ``embed_texts`` with
the new model against still-old-dim stored vectors — the result is
silently corrupted hybrid search. The gate raises
:class:`EmbeddingMigrationInProgress` so callers can degrade gracefully
(BM25-only for queries, empty-vectors for ingestion). The migration job
itself bypasses via a :class:`contextvars.ContextVar` token.

Single-pod scope (OSS install). Multi-pod deployments need Redis pub/sub
for cache invalidation across replicas — flagged in the docstring of
:func:`bust_embedding_settings_cache`.
"""

from __future__ import annotations

import contextvars
import logging
import time
from dataclasses import dataclass
from typing import Any

from beever_atlas.infra.config import Settings, get_settings

logger = logging.getLogger(__name__)


_CACHE_TTL_SECONDS = 5.0


@dataclass(frozen=True)
class EffectiveEmbeddingConfig:
    """Snapshot of the settings the embedding shim should use *right now*.

    Frozen so callers can pass it around without worrying about mutation.
    Reads come from env (Settings) overlaid with the MongoDB
    ``embedding_settings`` document and the encrypted DB-stored API key.
    """

    provider: str
    model: str
    dimensions: int
    rpm: int
    api_base: str
    task: str
    api_key: str
    dim_guard_enabled: bool


class EmbeddingMigrationInProgress(RuntimeError):
    """Raised by ``embed_texts`` while a re-embed migration is running.

    Carries an optional ``estimated_remaining_seconds`` so HTTP layers can
    surface a useful Retry-After. Callers should:
      * Query path → catch and degrade to BM25-only.
      * Pipeline path → catch and write empty vectors for the batch.
      * Sync API → reject with HTTP 409.
    """

    def __init__(
        self,
        message: str = "Embedding migration in progress",
        *,
        estimated_remaining_seconds: int | None = None,
    ) -> None:
        super().__init__(message)
        self.estimated_remaining_seconds = estimated_remaining_seconds


# Context-var that the re-embed script flips to True so the migration
# job's own embed calls bypass the gate. ContextVar (not a thread-local
# bool) so concurrent requests don't accidentally inherit the bypass.
_in_migration_context: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "embedding_in_migration_context",
    default=False,
)


# --- Cache state ----------------------------------------------------------
# Module-globals; OSS is single-pod single-process. The PUT handler busts
# the cache directly. Cache is intentionally TTL-bounded (not write-through)
# so a failed bust doesn't pin stale config forever.

_cached_config: EffectiveEmbeddingConfig | None = None
_cached_config_ts: float = 0.0
_cached_migration: bool | None = None
_cached_migration_ts: float = 0.0


def bust_embedding_settings_cache() -> None:
    """Invalidate both the effective-config cache and the migration-state cache.

    Called by ``api/embedding_settings.update_embedding_settings`` after a
    successful PUT so the very next ``embed_texts`` call reflects the
    operator's change without waiting for the 5s TTL.

    **Single-pod only.** Multi-pod deployments need a pub/sub broadcast
    here (Redis ``PUBLISH embedding_settings_changed`` + each pod
    subscribed). Out of OSS scope.
    """
    global _cached_config, _cached_config_ts, _cached_migration, _cached_migration_ts
    _cached_config = None
    _cached_config_ts = 0.0
    _cached_migration = None
    _cached_migration_ts = 0.0
    logger.debug("embedding_runtime: cache busted")


async def _load_db_overrides() -> dict[str, Any]:
    """Best-effort read of the MongoDB ``embedding_settings`` doc + secret.

    Returns the merged override dict (provider/model/dimensions/rpm/
    api_base/task + decrypted api_key). Empty dict on Mongo failure so the
    embed path keeps working off env fallback alone.
    """
    overrides: dict[str, Any] = {}
    try:
        from beever_atlas.stores import get_stores

        stores = get_stores()
    except Exception:  # noqa: BLE001 — stores not initialised (test paths)
        return overrides

    try:
        doc = await stores.mongodb.db["embedding_settings"].find_one({"_id": "embedding_settings"})
        if doc:
            for key in ("provider", "model", "dimensions", "rpm", "api_base", "task"):
                if doc.get(key) is not None:
                    overrides[key] = doc[key]
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embedding_runtime: db_overrides lookup failed (%s) — using env only",
            exc,
        )

    # Decrypt the encrypted API key if present. Failure is non-fatal —
    # the embed path falls back to env defaults (provider-default vars).
    try:
        from beever_atlas.api.embedding_settings import _decrypt_db_key

        db_key = await _decrypt_db_key()
        if db_key:
            overrides["api_key"] = db_key
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embedding_runtime: api_key decrypt failed (%s) — using env api_key",
            exc,
        )

    return overrides


def _build_effective(base: Settings, overrides: dict[str, Any]) -> EffectiveEmbeddingConfig:
    return EffectiveEmbeddingConfig(
        provider=overrides.get("provider", base.embedding_provider),
        model=overrides.get("model", base.embedding_model),
        dimensions=int(overrides.get("dimensions", base.embedding_dimensions)),
        rpm=int(overrides.get("rpm", base.embedding_rpm)),
        api_base=overrides.get("api_base", base.embedding_api_base),
        task=overrides.get("task", base.embedding_task),
        api_key=overrides.get("api_key", base.embedding_api_key),
        dim_guard_enabled=base.embedding_dim_guard,
    )


async def resolve_effective_settings(base: Settings | None = None) -> Settings:
    """Return a ``Settings`` clone with the live DB override doc applied
    over ``base`` (defaults to :func:`get_settings`).

    Used by both the runtime ``embed_texts`` path and the boot-time dim
    guard so the two code paths can't drift: every probe and every
    production embed call resolves provider/model/dimensions/api_key
    through the same merge, on top of the *same* env baseline that the
    caller chose.

    Pivots on the passed-in ``base`` (NOT the cached env Settings), so
    tests injecting a custom ``Settings`` get DB overrides applied to
    THEIR baseline rather than to the process env. Falls back to ``base``
    unchanged when DB resolution fails (Mongo unreachable, master key
    missing, etc.) so boot still proceeds.
    """
    if base is None:
        base = get_settings()
    try:
        overrides = await _load_db_overrides()
    except Exception as exc:  # noqa: BLE001
        # SECURITY: ``_load_db_overrides`` decrypts the embedding API key
        # internally (assigns ``overrides["api_key"] = db_key``). If the
        # function raises mid-execution, that inner frame holds the
        # plaintext ``db_key`` and the ``overrides`` dict containing it.
        # ``exc_info=True`` walks back through that frame's locals. Log
        # class + message only — same guard as ``F5`` /
        # ``provider.py:160-163`` / ``agent_credentials.py:84-86``.
        logger.warning(
            "embedding_runtime: resolve_effective_settings failed — falling back to env Settings (%s: %s)",
            type(exc).__name__,
            exc,
        )
        return base
    if not overrides:
        return base
    update: dict[str, Any] = {}
    if "provider" in overrides:
        update["embedding_provider"] = overrides["provider"]
    if "model" in overrides:
        update["embedding_model"] = overrides["model"]
    if "dimensions" in overrides:
        update["embedding_dimensions"] = int(overrides["dimensions"])
    if "rpm" in overrides:
        update["embedding_rpm"] = int(overrides["rpm"])
    if "api_base" in overrides:
        update["embedding_api_base"] = overrides["api_base"]
    if "task" in overrides:
        update["embedding_task"] = overrides["task"]
    if "api_key" in overrides:
        update["embedding_api_key"] = overrides["api_key"]
    if not update:
        return base
    return base.model_copy(update=update)


async def get_effective_embedding_settings() -> EffectiveEmbeddingConfig:
    """Return the live effective embedding configuration.

    Resolution order (highest precedence first):
      1. MongoDB ``embedding_settings`` document — UI-saved overrides.
      2. ``Settings`` env-derived values — including legacy ``JINA_*``
         aliases bridged in :func:`Settings._bridge_legacy_jina_aliases`.
      3. ``embedding_secret`` MongoDB doc → ``api_key`` (decrypted).
      4. ``settings.embedding_api_key`` env override (highest for keys
         specifically; matches ``_resolve_api_key`` in the shim).

    5-second in-process TTL cache. Bypassed via
    :func:`bust_embedding_settings_cache` after a successful PUT.
    """
    global _cached_config, _cached_config_ts

    now = time.monotonic()
    if _cached_config is not None and (now - _cached_config_ts) < _CACHE_TTL_SECONDS:
        return _cached_config

    base = get_settings()
    overrides = await _load_db_overrides()
    effective = _build_effective(base, overrides)

    _cached_config = effective
    _cached_config_ts = now
    return effective


async def is_migration_in_progress() -> bool:
    """True when the persisted ``embedding_meta.dimensions`` disagrees with
    the effective configured dimension AND Weaviate already holds rows.

    The first condition catches "operator just saved a new model"; the
    second prevents a false-positive on a fresh empty install where the
    initial dim guard hasn't yet rebooted to the new config.

    5-second TTL cache.

    Best-effort: returns ``False`` when MongoDB or Weaviate is
    unavailable rather than raising — gating decisions in callers should
    fail-open for availability.
    """
    global _cached_migration, _cached_migration_ts

    now = time.monotonic()
    if _cached_migration is not None and (now - _cached_migration_ts) < _CACHE_TTL_SECONDS:
        return _cached_migration

    in_progress = False
    try:
        from beever_atlas.stores import get_stores

        stores = get_stores()
        meta = await stores.mongodb.get_embedding_meta()
        if meta is None:
            in_progress = False
        else:
            effective = await get_effective_embedding_settings()
            if int(meta.get("dimensions") or 0) != effective.dimensions:
                # Different dim configured vs persisted — migration window.
                # Confirm Weaviate has rows; if not, this is just a fresh
                # config flip with nothing to migrate.
                try:
                    fact_count = await stores.weaviate.count_facts()
                except Exception:  # noqa: BLE001
                    # Weaviate unavailable — fail-open. The boot-time dim
                    # guard will catch any real mismatch on the next boot.
                    fact_count = 0
                in_progress = fact_count > 0
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embedding_runtime: migration-gate probe failed (%s) — fail-open",
            exc,
        )
        in_progress = False

    _cached_migration = in_progress
    _cached_migration_ts = now
    return in_progress


def in_migration_context() -> bool:
    """True if the current asyncio task is the re-embed migration job
    (set by :func:`scripts.reembed_facts.main` via the contextvar)."""
    return _in_migration_context.get()


def set_migration_context(value: bool) -> contextvars.Token[bool]:
    """Mark the current task as the migration job. Returns the token to
    restore the previous value via :func:`reset_migration_context`."""
    return _in_migration_context.set(value)


def reset_migration_context(token: contextvars.Token[bool]) -> None:
    _in_migration_context.reset(token)


__all__ = [
    "EffectiveEmbeddingConfig",
    "EmbeddingMigrationInProgress",
    "bust_embedding_settings_cache",
    "get_effective_embedding_settings",
    "in_migration_context",
    "is_migration_in_progress",
    "reset_migration_context",
    "resolve_effective_settings",
    "set_migration_context",
]
