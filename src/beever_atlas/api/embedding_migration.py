"""Re-embed migration API — the B-i re-home of the re-embed machinery.

The re-embed *machinery* (the ``scripts.reembed_facts`` job + the
``reembed_state`` checkpoint + the boot-time dim guard) is real backend
infra: it isn't part of the deprecated ``/api/settings/embedding`` config
surface, it just *lived* there because that's where the embedding settings
UI was. This router gives that machinery a **non-deprecated home** that:

  * reads the ``embedding`` **Assignment** + its **Endpoint** as the source
    of truth for the re-embed *target* (provider/model/dimensions/base_url),
  * writes that target *through* to the legacy ``embedding_settings`` Mongo
    doc as the re-embed job's *input channel* — because ``reembed_facts``
    still resolves its target from that doc (until the embedding *runtime*
    itself reads Assignments, which is a separate future change), and
  * shares the in-process job registry with the legacy
    ``/api/settings/embedding/migrate*`` endpoints (via
    ``services/embedding_migration_job``) so a re-embed triggered through
    either surface dedupes against the other.

This lets the frontend stop hitting deprecation-stamped routes and lets a
future Phase-5 cleanup delete the ``/api/settings/embedding/*`` config
read/write/test endpoints whole.

Endpoints (prefix ``/api/settings/embedding-migration``, NOT deprecation-
stamped):
  GET  /status   — current re-embed job state (mirrors the legacy
                   ``/migrate/status`` shape).
  GET  /state    — dim-mismatch detection: desired (Assignment-derived) vs
                   persisted (``embedding_meta``-recorded) config + whether
                   the desired provider supports the legacy re-embed path.
  POST /spawn    — server-side dual-write the Assignment's config into the
                   legacy ``embedding_settings`` doc + credential, then spawn
                   the re-embed job.
"""

from __future__ import annotations

import logging
import traceback

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from beever_atlas.llm import agent_credentials
from beever_atlas.llm.assignments import AssignmentStore
from beever_atlas.llm.endpoints import EndpointStore, preset_to_provider
from beever_atlas.llm.known_embedding_models import SUPPORTED_PROVIDERS
from beever_atlas.services.embedding_migration_job import (
    migration_status_snapshot,
    spawn_reembed_job,
)
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settings/embedding-migration",
    tags=["embedding-migration"],
)


# ─── Response models ───────────────────────────────────────────────────────


class ReembedStatusResponse(BaseModel):
    """Mirrors the legacy ``MigrateStatusResponse`` shape."""

    running: bool
    job_id: str | None = None
    stage: str | None = None
    processed: int | None = None
    total: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


class ReembedStateResponse(BaseModel):
    """Dim-mismatch detection: desired (Assignment) vs persisted (meta)."""

    migration_required: bool
    desired_provider: str | None = None
    desired_model: str | None = None
    desired_dimensions: int | None = None
    persisted_provider: str | None = None
    persisted_model: str | None = None
    persisted_dimensions: int | None = None
    fact_count: int | None = None
    # ``reembed_supported`` is False when the desired provider isn't a direct
    # embedding provider the legacy re-embed job accepts (e.g. an
    # ``litellm_proxy`` / ``custom`` endpoint). ``reason`` explains why so
    # the UI can disable the "Start re-embed" button with a helpful message.
    reembed_supported: bool = False
    reason: str | None = None


class ReembedSpawnResponse(BaseModel):
    job_id: str
    status: str  # "running" | "running_existing"


# ─── Helpers ───────────────────────────────────────────────────────────────


# The embedding model table uses ``gemini`` as the provider key; the Endpoint
# preset for Google's API is ``google_ai`` which ``preset_to_provider`` maps
# to LiteLLM's ``gemini/`` prefix already — but be explicit in case the
# mapping ever changes so the embedding-table lookup stays correct.
def _desired_provider_from_preset(preset: str) -> str:
    provider = preset_to_provider(preset)
    if provider == "google_ai":
        return "gemini"
    return provider


async def _load_embedding_assignment_and_endpoint():  # noqa: ANN202
    """Return ``(assignment, endpoint)`` for the ``embedding`` consumer.

    ``assignment`` is ``None`` when no ``embedding`` Assignment is
    configured; ``endpoint`` is ``None`` when the Assignment references an
    Endpoint that no longer exists.
    """
    stores = get_stores()
    assignment = await AssignmentStore(stores.mongodb).get("embedding")
    if assignment is None:
        return None, None
    endpoint = await EndpointStore(stores.mongodb).get(assignment.endpoint_id)
    return assignment, endpoint


# ─── Endpoints ─────────────────────────────────────────────────────────────


@router.get("/status", response_model=ReembedStatusResponse)
async def reembed_status() -> ReembedStatusResponse:
    snapshot = await migration_status_snapshot()
    return ReembedStatusResponse(**snapshot)


@router.get("/state", response_model=ReembedStateResponse)
async def reembed_state() -> ReembedStateResponse:
    assignment, endpoint = await _load_embedding_assignment_and_endpoint()

    if assignment is None:
        # No source of truth — nothing to re-embed toward.
        return ReembedStateResponse(
            migration_required=False,
            reembed_supported=False,
            reason="no embedding assignment configured",
        )

    desired_provider: str | None = None
    desired_model: str | None = assignment.model or None
    desired_dimensions: int | None = assignment.dimensions
    reason: str | None = None
    if endpoint is not None:
        desired_provider = _desired_provider_from_preset(endpoint.preset)
    else:
        reason = "embedding assignment references a missing endpoint"

    # Persisted (Weaviate-current) embedding meta.
    stores = get_stores()
    meta: dict = {}
    try:
        meta = (await stores.mongodb.get_embedding_meta()) or {}
    except Exception:  # noqa: BLE001
        logger.warning("embedding_migration: meta lookup failed", exc_info=True)
        meta = {}
    persisted_dimensions = meta.get("dimensions")
    persisted_provider = meta.get("provider")
    persisted_model = meta.get("model")

    # Best-effort fact count — if Weaviate is unreachable, leave it None.
    fact_count: int | None = None
    try:
        fact_count = await stores.weaviate.count_facts()
    except Exception:  # noqa: BLE001
        logger.debug(
            "embedding_migration: count_facts failed during GET /state (non-fatal)",
            exc_info=True,
        )

    # PR-η: authoritative dim check against actual Weaviate vectors. A failed
    # migration (or a wishful boot probe) can leave ``embedding_meta`` pointing
    # at a model that was never actually re-embedded. The on-disk vectors are
    # the ground truth — if their dim disagrees with meta, meta is stale and
    # we override ``persisted_dimensions`` so the UI sees the real mixed-dim
    # state and surfaces "Re-embed required".
    actual_dim: int | None = None
    if (fact_count or 0) > 0:
        try:
            actual_dim = await stores.weaviate.sample_fact_vector_dim()
        except Exception:  # noqa: BLE001
            logger.debug(
                "embedding_migration: sample_fact_vector_dim failed (non-fatal)",
                exc_info=True,
            )
    if actual_dim is not None and persisted_dimensions != actual_dim:
        logger.warning(
            "embedding_migration: embedding_meta dim=%s disagrees with actual "
            "Weaviate vector dim=%s — using actual dim as source of truth "
            "(meta was likely corrupted by a failed migration or boot probe)",
            persisted_dimensions,
            actual_dim,
        )
        persisted_dimensions = actual_dim

    migration_required = bool(
        persisted_dimensions is not None
        and desired_dimensions is not None
        and persisted_dimensions != desired_dimensions
        and (fact_count or 0) > 0
    )

    reembed_supported = desired_provider in SUPPORTED_PROVIDERS
    if not reembed_supported and reason is None and desired_provider is not None:
        preset_label = endpoint.preset if endpoint is not None else "?"
        reason = (
            f"endpoint preset {preset_label!r} isn't a direct embedding provider "
            "— re-embed not yet supported via proxy endpoints"
        )

    return ReembedStateResponse(
        migration_required=migration_required,
        desired_provider=desired_provider,
        desired_model=desired_model,
        desired_dimensions=desired_dimensions,
        persisted_provider=persisted_provider,
        persisted_model=persisted_model,
        persisted_dimensions=persisted_dimensions,
        fact_count=fact_count,
        reembed_supported=reembed_supported,
        reason=reason,
    )


@router.post("/spawn", response_model=ReembedSpawnResponse)
async def reembed_spawn() -> ReembedSpawnResponse:
    assignment, endpoint = await _load_embedding_assignment_and_endpoint()

    if assignment is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "no_embedding_assignment"},
        )
    if endpoint is None:
        raise HTTPException(
            status_code=422,
            detail={"error": "endpoint_not_found", "endpoint_id": assignment.endpoint_id},
        )

    desired_provider = _desired_provider_from_preset(endpoint.preset)
    desired_model = assignment.model
    desired_dimensions = assignment.dimensions
    if desired_provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_embedding_provider_for_reembed",
                "provider": desired_provider,
                "endpoint_preset": endpoint.preset,
            },
        )

    # Server-side B-i dual-write + spawn. We import the legacy-doc writer
    # helpers (``_persist_settings_doc`` / ``_persist_api_key``) from
    # ``embedding_settings`` on purpose — they are the canonical writers for
    # the legacy ``embedding_settings`` Mongo doc + the encrypted-credential
    # store, and ``reembed_facts`` reads its target from exactly that doc.
    # Re-implementing them here would risk drift. Imported lazily to avoid a
    # module-load circular import (embedding_settings ↔ this module's
    # neighbours).
    from beever_atlas.api import embedding_settings
    from beever_atlas.llm import embeddings as embeddings_runtime

    try:
        await embedding_settings._persist_settings_doc(
            {
                "provider": desired_provider,
                "model": desired_model,
                "dimensions": desired_dimensions,
                "api_base": endpoint.base_url or None,
            }
        )
        # Persist the credential the Endpoint already holds (decrypted at
        # boot, cached in ``agent_credentials``). Only the api_key shape
        # (a plain str) maps onto the legacy single-key store; aws_iam /
        # google_sa blobs (dict) and ``auth_type=none`` (None) are left
        # alone — the legacy re-embed only supports key-based providers.
        plaintext = agent_credentials.get_runtime_credential(endpoint.id)
        if isinstance(plaintext, str):
            await embedding_settings._persist_api_key(plaintext)
            # Mirror what ``update_embedding_settings`` does — seed the
            # in-process runtime key so the re-embed job's very next embed
            # call uses it without waiting for a cache miss.
            embeddings_runtime.set_runtime_db_api_key(plaintext)

        job_id, status = spawn_reembed_job()
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        # Log the full traceback server-side; surface ONLY the exception
        # class to the client (raw strings can carry internal URLs /
        # credential fragments).
        logger.error(
            "embedding_migration: re-embed spawn failed — %s\n%s",
            exc,
            traceback.format_exc(),
        )
        raise HTTPException(
            status_code=503,
            detail={"error": "reembed_spawn_failed", "type": type(exc).__name__},
        ) from exc

    return ReembedSpawnResponse(job_id=job_id, status=status)


__all__ = ["router"]
