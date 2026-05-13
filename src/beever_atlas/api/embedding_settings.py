"""Embedding-provider settings API (PR-E).

Endpoints:
  GET  /api/settings/embedding             — current effective config + masked key.
  PUT  /api/settings/embedding             — update config (encrypted at rest).
  POST /api/settings/embedding/test        — probe-embed with candidate creds.
  POST /api/settings/embedding/migrate     — spawn re-embed job.
  GET  /api/settings/embedding/migrate/status — current job state.

Security contract:
  * Plaintext API keys are NEVER returned. ``GET`` exposes only a masked
    prefix/suffix and a ``has_api_key`` boolean.
  * ``PUT`` accepts a plaintext ``api_key`` in the request body, encrypts
    it with the existing ``CredentialEncryptor`` before persisting, and
    immediately discards the plaintext.
  * Embedding calls decrypt only inside the shim, immediately before the
    LiteLLM round-trip.
"""

from __future__ import annotations

import base64
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from beever_atlas.api._deprecation import deprecated_route
from pydantic import BaseModel, Field

from beever_atlas.infra.config import get_settings
from beever_atlas.llm.known_embedding_models import (
    KNOWN_EMBEDDING_MODELS,
    SUPPORTED_PROVIDERS,
    is_known,
)

# The re-embed in-process registry + spawn/status helpers now live in a
# shared service module so this legacy (deprecation-stamped) router AND the
# new ``api/embedding_migration.py`` router share *one* registry. The
# ``_active_migration`` dict is re-exported here (intentionally imported but
# unused at module level — the F401 suppression below) so existing
# callers/tests that reference ``embedding_settings._active_migration`` keep
# working.
from beever_atlas.services.embedding_migration_job import _active_migration  # noqa: F401
from beever_atlas.services.embedding_migration_job import (
    migration_status_snapshot,
    spawn_reembed_job,
)
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/settings/embedding",
    tags=["embedding-settings"],
    dependencies=[Depends(deprecated_route("/api/settings/endpoints"))],
)


# ─── Request / response models ─────────────────────────────────────────────


class EmbeddingSettingsResponse(BaseModel):
    provider: str
    model: str
    dimensions: int
    rpm: int
    api_base: str
    task: str
    has_api_key: bool
    api_key_masked: str
    source: str  # "db" | "env" | "default"
    dim_guard_enabled: bool
    last_probe_at: str | None = None
    last_probe_ok: bool | None = None
    last_probe_error: str | None = None
    # The state needed by the UI's "Re-embed required" banner — surfaces
    # the case where effective config differs from what's actually in
    # Weaviate, which previously had no UI affordance and forced
    # operators to either pop the dim-mismatch modal by editing config
    # or hit the migrate endpoint manually.
    persisted_provider: str | None = None
    persisted_model: str | None = None
    persisted_dimensions: int | None = None
    fact_count: int | None = None
    migration_required: bool = False


class UpdateEmbeddingRequest(BaseModel):
    provider: str | None = None
    model: str | None = None
    dimensions: int | None = Field(default=None, ge=1, le=8192)
    rpm: int | None = Field(default=None, ge=1, le=10000)
    api_base: str | None = None
    task: str | None = None
    api_key: str | None = None
    # Required when the new dim differs from ``embedding_meta.dimensions`` and
    # Weaviate is non-empty. Forces an explicit acknowledgement of the
    # required re-embed migration.
    confirm_migration: bool = False


class TestConnectionResponse(BaseModel):
    ok: bool
    dimensions: int | None = None
    latency_ms: int | None = None
    provider: str
    model: str
    error: str | None = None


class MigrateRequest(BaseModel):
    """No body required — config is read from current Settings + DB."""

    pass


class MigrateResponse(BaseModel):
    job_id: str
    status: str  # "running" | "running_existing"


class MigrateStatusResponse(BaseModel):
    running: bool
    job_id: str | None = None
    stage: str | None = None
    processed: int | None = None
    total: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────


def _mask_key(value: str) -> str:
    """Return ``<first 4>...<last 4>`` for an API key, or ``***`` if short.

    Avoids re-implementing prefix knowledge for every provider; we just
    surface enough to confirm "yes the right key is loaded" without
    leaking it.
    """
    if not value:
        return ""
    if len(value) < 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


async def _decrypt_db_key() -> str | None:
    """Return the plaintext API key persisted in MongoDB, or None.

    Raises HTTP 503 if the master key is unconfigured.
    """
    stores = get_stores()
    secret = await stores.mongodb.get_embedding_secret()
    if not secret:
        return None
    try:
        from beever_atlas.infra.crypto import decrypt_credentials

        ciphertext = base64.b64decode(secret["ciphertext_b64"])
        iv = base64.b64decode(secret["iv_b64"])
        tag = base64.b64decode(secret["tag_b64"])
        return decrypt_credentials(ciphertext, iv, tag).get("api_key")
    except RuntimeError as exc:
        # Master key missing or invalid — fail closed.
        raise HTTPException(
            status_code=503,
            detail={"error": "credential_encryptor_unavailable", "message": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("embedding_settings: failed to decrypt stored key")
        raise HTTPException(status_code=503, detail={"error": "credential_decrypt_failed"}) from exc


async def _resolve_effective_settings() -> tuple[Any, str, str]:
    """Return ``(effective_settings_view, source, masked_key)``.

    ``effective_settings_view`` is a dict-like snapshot reflecting:
      * Env (Settings) — baseline.
      * DB overrides   — live edits via PUT.
      * DB-stored encrypted API key — decrypted only for ``masked_key`` here.

    ``source`` is one of ``"db"`` | ``"env"`` | ``"default"`` describing the
    dominant source. ``"default"`` means neither a DB override nor an env
    override was found and the operator hasn't configured anything.
    """
    cfg = get_settings()
    stores = get_stores()

    # Look for a DB override doc.
    db_doc = None
    try:
        db_doc = await stores.mongodb.db["embedding_settings"].find_one(
            {"_id": "embedding_settings"}
        )
    except Exception:  # noqa: BLE001
        logger.warning("embedding_settings: db lookup failed", exc_info=True)
    if db_doc:
        db_doc.pop("_id", None)

    import os

    env_set = any(
        os.environ.get(v)
        for v in (
            "EMBEDDING_PROVIDER",
            "EMBEDDING_MODEL",
            "EMBEDDING_DIMENSIONS",
            "EMBEDDING_RPM",
            "EMBEDDING_API_BASE",
            "EMBEDDING_API_KEY",
        )
    )

    # ``view`` overlays DB onto env.
    view: dict[str, Any] = {
        "provider": cfg.embedding_provider,
        "model": cfg.embedding_model,
        "dimensions": cfg.embedding_dimensions,
        "rpm": cfg.embedding_rpm,
        "api_base": cfg.embedding_api_base,
        "task": cfg.embedding_task,
        "dim_guard_enabled": cfg.embedding_dim_guard,
    }
    if db_doc:
        for key in ("provider", "model", "dimensions", "rpm", "api_base", "task"):
            if db_doc.get(key) is not None:
                view[key] = db_doc[key]

    if db_doc:
        source = "db"
    elif env_set:
        source = "env"
    else:
        source = "default"

    # Determine masked key.
    masked_key = ""
    has_api_key = False
    if cfg.embedding_api_key:
        masked_key = _mask_key(cfg.embedding_api_key)
        has_api_key = True
    else:
        try:
            db_key = await _decrypt_db_key()
        except HTTPException:
            # Master key unavailable but we still want to expose a hint.
            db_key = None
        if db_key:
            masked_key = _mask_key(db_key)
            has_api_key = True

    view["masked_key"] = masked_key
    view["has_api_key"] = has_api_key
    return view, source, masked_key


async def _get_meta() -> dict[str, Any]:
    stores = get_stores()
    try:
        return (await stores.mongodb.get_embedding_meta()) or {}
    except Exception:  # noqa: BLE001
        logger.warning("embedding_settings: meta lookup failed", exc_info=True)
        return {}


# ─── Endpoints ────────────────────────────────────────────────────────────


@router.get("", response_model=EmbeddingSettingsResponse)
async def get_embedding_settings() -> EmbeddingSettingsResponse:
    view, source, masked_key = await _resolve_effective_settings()
    meta = await _get_meta()

    # Compute migration_required: configured dim differs from what's in
    # storage AND there's actually data to re-embed. Best-effort — if
    # Weaviate is unreachable, the GET response still works (UI just
    # won't show the banner).
    persisted_dim = meta.get("dimensions") if meta else None
    fact_count: int | None = None
    try:
        fact_count = await get_stores().weaviate.count_facts()
    except Exception:  # noqa: BLE001
        logger.debug(
            "embedding_settings: count_facts failed during GET (non-fatal)",
            exc_info=True,
        )
    migration_required = bool(
        persisted_dim is not None
        and persisted_dim != view["dimensions"]
        and fact_count is not None
        and fact_count > 0
    )

    return EmbeddingSettingsResponse(
        provider=view["provider"],
        model=view["model"],
        dimensions=view["dimensions"],
        rpm=view["rpm"],
        api_base=view["api_base"],
        task=view["task"],
        has_api_key=view["has_api_key"],
        api_key_masked=masked_key,
        source=source,
        dim_guard_enabled=view["dim_guard_enabled"],
        last_probe_at=meta.get("last_probe_at"),
        last_probe_ok=meta.get("last_probe_ok"),
        last_probe_error=meta.get("last_probe_error"),
        persisted_provider=meta.get("provider"),
        persisted_model=meta.get("model"),
        persisted_dimensions=persisted_dim,
        fact_count=fact_count,
        migration_required=migration_required,
    )


def _validate_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "unsupported_provider",
                "supported": list(SUPPORTED_PROVIDERS),
            },
        )


def _validate_dimensions_against_known(provider: str, model: str, dimensions: int) -> None:
    """If the model is in our known table, the configured dim must match."""
    spec = KNOWN_EMBEDDING_MODELS.get(f"{provider}/{model}")
    if spec is not None and spec["dim"] != dimensions:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "dimension_mismatch_known_model",
                "model": f"{provider}/{model}",
                "expected_dim": spec["dim"],
                "supplied_dim": dimensions,
            },
        )


async def _check_dim_change_requires_migration(new_dim: int) -> None:
    """422 when changing ``dimensions`` against a populated Weaviate
    without ``confirm_migration: true``."""
    meta = await _get_meta()
    persisted_dim = meta.get("dimensions")
    if persisted_dim is None or persisted_dim == new_dim:
        return
    stores = get_stores()
    try:
        fact_count = await stores.weaviate.count_facts()
    except Exception:  # noqa: BLE001
        # Weaviate down — skip the check, dim guard at next boot will catch.
        return
    if fact_count > 0:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "dim_mismatch_requires_migration",
                "current_dim": persisted_dim,
                "new_dim": new_dim,
                "fact_count": fact_count,
            },
        )


async def _persist_settings_doc(updates: dict[str, Any]) -> None:
    stores = get_stores()
    payload = {k: v for k, v in updates.items() if v is not None and k != "api_key"}
    if not payload and "api_key" not in updates:
        return
    payload["updated_at"] = datetime.now(tz=UTC).isoformat()
    await stores.mongodb.db["embedding_settings"].update_one(
        {"_id": "embedding_settings"},
        {"$set": payload},
        upsert=True,
    )


async def _persist_api_key(plaintext: str | None) -> None:
    """Encrypt and persist; ``None`` clears the stored value."""
    stores = get_stores()
    if plaintext is None:
        return  # PUT silence on api_key means "leave existing alone"
    if plaintext == "":
        # Empty string explicitly clears the stored key.
        await stores.mongodb.clear_embedding_secret()
        return
    try:
        from beever_atlas.infra.crypto import encrypt_credentials

        ciphertext, iv, tag = encrypt_credentials({"api_key": plaintext})
    except RuntimeError as exc:
        raise HTTPException(
            status_code=503,
            detail={"error": "credential_encryptor_unavailable", "message": str(exc)},
        ) from exc

    await stores.mongodb.set_embedding_secret(
        ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
        iv_b64=base64.b64encode(iv).decode("ascii"),
        tag_b64=base64.b64encode(tag).decode("ascii"),
    )


@router.put("", response_model=EmbeddingSettingsResponse)
async def update_embedding_settings(req: UpdateEmbeddingRequest) -> EmbeddingSettingsResponse:
    view, _, _ = await _resolve_effective_settings()

    new_provider = req.provider if req.provider is not None else view["provider"]
    new_model = req.model if req.model is not None else view["model"]
    new_dimensions = req.dimensions if req.dimensions is not None else view["dimensions"]

    _validate_provider(new_provider)
    if is_known(new_provider, new_model):
        _validate_dimensions_against_known(new_provider, new_model, new_dimensions)

    if req.dimensions is not None and not req.confirm_migration:
        await _check_dim_change_requires_migration(req.dimensions)

    # Persist non-secret fields.
    await _persist_settings_doc(
        {
            "provider": req.provider,
            "model": req.model,
            "dimensions": req.dimensions,
            "rpm": req.rpm,
            "api_base": req.api_base,
            "task": req.task,
        }
    )

    # Persist encrypted API key separately.
    if req.api_key is not None:
        await _persist_api_key(req.api_key)
        # Also seed the in-process runtime key so the very next embed
        # call uses it without waiting for the next cache miss.
        from beever_atlas.llm.embeddings import set_runtime_db_api_key

        set_runtime_db_api_key(req.api_key or None)

    # Bust the live-config cache so the next embed_texts() picks up the
    # new settings without restart or 5-second TTL wait. Layer 3 of the
    # provider-pluggable embedding feature.
    from beever_atlas.llm.embedding_runtime import bust_embedding_settings_cache

    bust_embedding_settings_cache()

    return await get_embedding_settings()


@router.post("/test", response_model=TestConnectionResponse)
async def test_embedding_connection(req: UpdateEmbeddingRequest) -> TestConnectionResponse:
    """Probe-embed using the candidate (or persisted) credentials.

    Never persists. Returns dimensions on success, error string on failure.
    """
    view, _, _ = await _resolve_effective_settings()

    candidate_provider = req.provider if req.provider is not None else view["provider"]
    candidate_model = req.model if req.model is not None else view["model"]
    candidate_dim = req.dimensions if req.dimensions is not None else view["dimensions"]
    candidate_rpm = req.rpm if req.rpm is not None else view["rpm"]
    candidate_api_base = req.api_base if req.api_base is not None else view["api_base"]
    candidate_task = req.task if req.task is not None else view["task"]

    if candidate_provider not in SUPPORTED_PROVIDERS:
        return TestConnectionResponse(
            ok=False,
            provider=candidate_provider,
            model=candidate_model,
            error=f"unsupported provider {candidate_provider!r}",
        )

    # Resolve effective key: explicit body > db > env.
    effective_key: str | None = req.api_key
    if effective_key is None:
        effective_key = await _decrypt_db_key()
    if effective_key is None:
        cfg = get_settings()
        effective_key = cfg.embedding_api_key or None

    # Build a one-off Settings view (no env clobber, no global state mutation).
    base_settings = get_settings()
    test_settings = base_settings.model_copy(
        update={
            "embedding_provider": candidate_provider,
            "embedding_model": candidate_model,
            "embedding_dimensions": candidate_dim,
            "embedding_rpm": candidate_rpm,
            "embedding_api_base": candidate_api_base,
            "embedding_api_key": effective_key or "",
            "embedding_task": candidate_task,
        }
    )

    from beever_atlas.llm.embedding_health import _run_probe

    health = await _run_probe(test_settings)
    return TestConnectionResponse(
        ok=health.ok,
        dimensions=health.dim,
        latency_ms=health.latency_ms,
        provider=candidate_provider,
        model=candidate_model,
        error=health.error,
    )


# ─── Re-embed migration job (PR-F) ─────────────────────────────────────────
# The in-process registry + spawn-the-job + status-snapshot logic now lives
# in ``beever_atlas.services.embedding_migration_job`` (imported at module
# top) so both this legacy (deprecation-stamped) router AND the new
# ``api/embedding_migration.py`` router share *one* registry: a re-embed
# triggered via either surface must dedupe against the other, and a
# ``/status`` poll from either must reflect it. The route paths + response
# shapes here are unchanged — zero behaviour change for the legacy
# endpoints; they just delegate now.


@router.post("/migrate", response_model=MigrateResponse)
async def start_migration(req: MigrateRequest) -> MigrateResponse:
    job_id, status = spawn_reembed_job()
    return MigrateResponse(job_id=job_id, status=status)


@router.get("/migrate/status", response_model=MigrateStatusResponse)
async def migration_status() -> MigrateStatusResponse:
    snapshot = await migration_status_snapshot()
    return MigrateStatusResponse(**snapshot)
