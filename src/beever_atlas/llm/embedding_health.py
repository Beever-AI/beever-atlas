"""Boot-time embedding probe + dimension guard (PR-C).

Runs exactly once during ``init_llm_provider``:

  1. Issue a single ``embed_texts(["__probe__"])`` call against the
     configured provider/model. Confirm the returned vector matches the
     configured ``EMBEDDING_DIMENSIONS``.
  2. Compare against the persisted ``embedding_meta`` record in MongoDB.
     If the configured dimension differs AND Weaviate already has rows
     under the persisted dimension, refuse to boot — mixing dimensions
     silently corrupts hybrid search and there is no schema-side
     enforcement.
  3. Update ``embedding_meta`` so the next boot has a fresh comparison
     point.

Operator overrides:
  ``EMBEDDING_DIM_GUARD=false`` — log loud WARN per boot but allow start.

Failure modes that are NOT fatal:
  * Weaviate unreachable     → log WARN "dim-guard skipped", continue.
  * MongoDB unreachable      → same. The probe still runs, just isn't
                               persisted.
  * Probe round-trip fails   → that's the point of the guard. Bubble up
                               unless the operator explicitly disabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from beever_atlas.infra.config import Settings

logger = logging.getLogger(__name__)


class EmbeddingDimensionMismatch(RuntimeError):
    """Raised when the configured dim disagrees with persisted dim and
    Weaviate already holds rows. Aborts startup unless overridden.
    """


@dataclass
class EmbeddingHealth:
    ok: bool
    dim: int | None
    latency_ms: int
    error: str | None = None


async def _run_probe(settings: Settings) -> EmbeddingHealth:
    """One-shot probe call. Caller decides what to do on failure."""
    import time

    from beever_atlas.llm.embeddings import embed_texts

    start = time.monotonic()
    try:
        vectors = await embed_texts(["__probe__"], settings=settings)
    except Exception as exc:  # noqa: BLE001 — probe must report, not raise.
        return EmbeddingHealth(
            ok=False,
            dim=None,
            latency_ms=int((time.monotonic() - start) * 1000),
            error=str(exc),
        )
    if not vectors or not vectors[0]:
        return EmbeddingHealth(
            ok=False,
            dim=0,
            latency_ms=int((time.monotonic() - start) * 1000),
            error="probe returned empty vector",
        )
    return EmbeddingHealth(
        ok=True,
        dim=len(vectors[0]),
        latency_ms=int((time.monotonic() - start) * 1000),
    )


async def _safe_count_facts(stores: Any) -> int | None:
    """Best-effort Weaviate count. Returns ``None`` when Weaviate is
    unavailable so the guard knows to skip rather than treat the install
    as empty."""
    try:
        return await stores.weaviate.count_facts()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embedding_health: Weaviate unavailable during dim-guard "
            "(skipping fact count check): %s",
            exc,
        )
        return None


async def _safe_get_meta(stores: Any) -> dict[str, Any] | None:
    """Best-effort MongoDB read of the embedding_meta record."""
    try:
        return await stores.mongodb.get_embedding_meta()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embedding_health: MongoDB unavailable during dim-guard "
            "(skipping persisted-meta check): %s",
            exc,
        )
        return None


async def _safe_set_meta(
    stores: Any,
    *,
    provider: str,
    model: str,
    dimensions: int,
    ok: bool,
    error: str | None,
) -> None:
    try:
        await stores.mongodb.set_embedding_meta(
            provider=provider,
            model=model,
            dimensions=dimensions,
            ok=ok,
            error=error,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "embedding_health: failed to persist embedding_meta (non-fatal): %s",
            exc,
        )


async def probe_and_validate(settings: Settings, stores: Any) -> EmbeddingHealth:
    """Run the boot-time probe + dimension guard.

    Returns the ``EmbeddingHealth`` from the probe call. On a fatal mismatch
    raises ``EmbeddingDimensionMismatch`` with a human-readable message
    pointing at the migration runbook.

    The ``stores`` argument is the ``StoreClients`` bundle so this module
    can stay testable without spinning up the singleton.

    The ``settings`` argument is the env-derived :class:`Settings` snapshot.
    Internally we resolve the *effective* settings (env merged with the
    MongoDB ``embedding_settings`` override doc) so a user who switched
    provider via the UI doesn't have to also edit ``.env`` for boot to
    pass. The runtime ``embed_texts`` path uses the same resolver, so the
    probe and production embed calls cannot diverge.
    """
    from beever_atlas.llm.embedding_runtime import resolve_effective_settings

    effective_settings = await resolve_effective_settings(settings)
    if (
        effective_settings.embedding_provider != settings.embedding_provider
        or effective_settings.embedding_model != settings.embedding_model
        or effective_settings.embedding_dimensions != settings.embedding_dimensions
    ):
        logger.info(
            "embedding_health: probing effective config (env+DB merged): "
            "%s/%s @ %d  (env baseline was %s/%s @ %d)",
            effective_settings.embedding_provider,
            effective_settings.embedding_model,
            effective_settings.embedding_dimensions,
            settings.embedding_provider,
            settings.embedding_model,
            settings.embedding_dimensions,
        )

    health = await _run_probe(effective_settings)

    if not health.ok:
        # Probe failure mode handling — PR-η:
        #
        # The boot guard's job is to prevent **data corruption** (a wrong-dim
        # model silently overwriting indexed vectors). A transport / config
        # failure (model not found, auth failure, network issue) is NOT a
        # corruption risk — no vectors are being produced, so nothing is
        # being mixed. Crashing the whole app on a config error is hostile:
        # the operator can't reach the UI to fix the model they typed wrong.
        #
        # We deliberately do NOT touch ``embedding_meta`` here. The meta doc
        # is the source of truth for **what's actually in Weaviate**. Writing
        # the configured-but-unvalidated model name into it corrupts that
        # source — a future ``/state`` lookup would see "persisted_dim ==
        # desired_dim" and report ``migration_required=False`` even though
        # no migration has run and the stored vectors are at a different dim.
        # That's exactly the bug PR-η is fixing.
        #
        # The probe error is already returned in ``health.error`` and logged
        # here; the UI surfaces it via ``/embedding-migration/state`` which
        # cross-checks ``embedding_meta`` against actual Weaviate vector dim.
        logger.error(
            "embedding_health: probe failed — app will start in degraded mode "
            "(no embeddings until config is fixed in Settings → Embedding). "
            "embedding_meta intentionally not touched (preserving source of "
            "truth for stored vector dim). Underlying error: %s",
            health.error,
        )
        return health

    # Probe succeeded with some dimension. Compare against effective config.
    if health.dim != effective_settings.embedding_dimensions:
        msg = (
            f"Provider returned vectors of length {health.dim} but "
            f"EMBEDDING_DIMENSIONS={effective_settings.embedding_dimensions}. "
            f"Update EMBEDDING_DIMENSIONS to {health.dim} or pick a model "
            f"that produces {effective_settings.embedding_dimensions}-dim vectors."
        )
        if not effective_settings.embedding_dim_guard:
            logger.warning("embedding_health: %s (DIM_GUARD off — continuing)", msg)
        else:
            raise EmbeddingDimensionMismatch(msg)

    # Probe matches config. Now compare against persisted meta + Weaviate.
    persisted = await _safe_get_meta(stores)
    fact_count = await _safe_count_facts(stores)

    ui_initiated_migration = (
        effective_settings.embedding_provider != settings.embedding_provider
        or effective_settings.embedding_model != settings.embedding_model
        or effective_settings.embedding_dimensions != settings.embedding_dimensions
    )

    if persisted is not None and fact_count is not None and fact_count > 0:
        persisted_dim = persisted.get("dimensions")
        if persisted_dim is not None and persisted_dim != effective_settings.embedding_dimensions:
            msg = (
                f"EmbeddingDimensionMismatch:\n"
                f"  Configured:  {effective_settings.embedding_provider}/{effective_settings.embedding_model} @ {effective_settings.embedding_dimensions}\n"
                f"  Persisted:   {persisted.get('provider')}/{persisted.get('model')} @ {persisted_dim}\n"
                f"  Weaviate has {fact_count:,} stored facts at the persisted dimension.\n\n"
                f"  Mixing dimensions will silently corrupt hybrid search.\n"
                f"  Either:\n"
                f"    1. Revert EMBEDDING_* to the persisted model, OR\n"
                f"    2. Run `make reembed-all` to rebuild the vector indexes.\n\n"
                f"  See docs/runbooks/embedding-migration.md\n"
                f"  Override at your own risk: EMBEDDING_DIM_GUARD=false"
            )
            if not effective_settings.embedding_dim_guard:
                logger.warning(
                    "embedding_health: dim mismatch with %d stored facts — "
                    "continuing because EMBEDDING_DIM_GUARD=false: %s",
                    fact_count,
                    msg,
                )
            elif ui_initiated_migration:
                # The user already went through the UI confirm-migration
                # flow (env baseline differs from effective settings, which
                # means a UI-saved override is active and the PUT handler's
                # `_check_dim_change_requires_migration` gate enforced
                # `confirm_migration: true` before persisting the override).
                # The runtime `is_migration_in_progress` gate already blocks
                # query/ingest embed calls until the re-embed completes,
                # so boot can proceed — operators need to reach the UI to
                # finish the migration. We deliberately do NOT update
                # ``embedding_meta`` here: persisted state stays pinned to
                # the old (still-on-disk) vectors so the runtime gate keeps
                # firing until the migration job calls ``set_embedding_meta``
                # itself at completion.
                logger.warning(
                    "embedding_health: UI-initiated migration pending — "
                    "boot allowed, queries will degrade to BM25 until "
                    "re-embed completes. %s",
                    msg,
                )
                return health
            else:
                raise EmbeddingDimensionMismatch(msg)

    # Everything checks out — update the persisted meta and let startup proceed.
    await _safe_set_meta(
        stores,
        provider=effective_settings.embedding_provider,
        model=effective_settings.embedding_model,
        dimensions=effective_settings.embedding_dimensions,
        ok=True,
        error=None,
    )
    logger.info(
        "embedding_health: probe ok provider=%s model=%s dim=%d latency_ms=%d",
        effective_settings.embedding_provider,
        effective_settings.embedding_model,
        health.dim,
        health.latency_ms,
    )
    return health


__all__ = [
    "EmbeddingDimensionMismatch",
    "EmbeddingHealth",
    "probe_and_validate",
]
