"""Policy CRUD and preset API endpoints."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.models.sync_policy import (
    ChannelPolicy,
    ConsolidationConfig,
    IngestionConfig,
    SyncConfig,
)
from beever_atlas.services.policy_presets import get_preset_config, list_presets
from beever_atlas.services.policy_resolver import resolve_policy
from beever_atlas.stores import get_stores

router = APIRouter(prefix="/api", tags=["policies"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class PolicyUpdateRequest(BaseModel):
    preset: str | None = None
    sync: SyncConfig | None = None
    ingestion: IngestionConfig | None = None
    consolidation: ConsolidationConfig | None = None
    enabled: bool | None = None


class GlobalDefaultsUpdateRequest(BaseModel):
    sync: SyncConfig | None = None
    ingestion: IngestionConfig | None = None
    consolidation: ConsolidationConfig | None = None
    max_concurrent_syncs: int | None = None


class BulkPolicyRequest(BaseModel):
    channel_ids: list[str]
    preset: str


# ---------------------------------------------------------------------------
# Cron validation
# ---------------------------------------------------------------------------


def _validate_cron(expression: str) -> None:
    """Validate a cron expression. Raises HTTPException on invalid."""
    parts = expression.strip().split()
    if len(parts) != 5:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid cron expression: expected 5 fields, got {len(parts)}",
        )


def _validate_policy_crons(sync: SyncConfig | None, consolidation: ConsolidationConfig | None):
    """Validate cron expressions if present in the config."""
    if sync and sync.cron_expression:
        _validate_cron(sync.cron_expression)
    if consolidation and consolidation.cron_expression:
        _validate_cron(consolidation.cron_expression)


# ---------------------------------------------------------------------------
# Helper to build response
# ---------------------------------------------------------------------------


async def _policy_response(channel_id: str, policy: ChannelPolicy) -> dict:
    stores = get_stores()
    defaults = await stores.mongodb.get_global_defaults()
    effective = resolve_policy(policy, defaults)
    return {
        "channel_id": channel_id,
        "preset": policy.preset,
        "policy": {
            "sync": policy.sync.model_dump(mode="json"),
            "ingestion": policy.ingestion.model_dump(mode="json"),
            "consolidation": policy.consolidation.model_dump(mode="json"),
        },
        "effective": {
            "sync": effective.sync.model_dump(mode="json"),
            "ingestion": effective.ingestion.model_dump(mode="json"),
            "consolidation": effective.consolidation.model_dump(mode="json"),
        },
        "enabled": policy.enabled,
        "syncs_since_last_consolidation": policy.syncs_since_last_consolidation,
        "created_at": policy.created_at.isoformat(),
        "updated_at": policy.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Global defaults
# ---------------------------------------------------------------------------


@router.get("/policies/defaults")
async def get_global_defaults() -> dict:
    """Return the current global policy defaults."""
    stores = get_stores()
    defaults = await stores.mongodb.get_global_defaults()
    return {
        "sync": defaults.sync.model_dump(mode="json"),
        "ingestion": defaults.ingestion.model_dump(mode="json"),
        "consolidation": defaults.consolidation.model_dump(mode="json"),
        "max_concurrent_syncs": defaults.max_concurrent_syncs,
        "updated_at": defaults.updated_at.isoformat(),
    }


@router.put("/policies/defaults")
async def update_global_defaults(body: GlobalDefaultsUpdateRequest) -> dict:
    """Update the global policy defaults."""
    stores = get_stores()
    defaults = await stores.mongodb.get_global_defaults()

    if body.sync:
        _validate_policy_crons(body.sync, None)
        for field_name in SyncConfig.model_fields:
            val = getattr(body.sync, field_name)
            if val is not None:
                setattr(defaults.sync, field_name, val)

    if body.ingestion:
        for field_name in IngestionConfig.model_fields:
            val = getattr(body.ingestion, field_name)
            if val is not None:
                setattr(defaults.ingestion, field_name, val)

    if body.consolidation:
        _validate_policy_crons(None, body.consolidation)
        for field_name in ConsolidationConfig.model_fields:
            val = getattr(body.consolidation, field_name)
            if val is not None:
                setattr(defaults.consolidation, field_name, val)

    if body.max_concurrent_syncs is not None:
        defaults.max_concurrent_syncs = body.max_concurrent_syncs

    updated = await stores.mongodb.update_global_defaults(defaults)
    logger.info("Policies API: global defaults updated")
    # Invalidate the in-process cache so subsequent resolves pick up changes
    from beever_atlas.services.policy_resolver import invalidate_defaults_cache
    invalidate_defaults_cache()
    return {
        "sync": updated.sync.model_dump(mode="json"),
        "ingestion": updated.ingestion.model_dump(mode="json"),
        "consolidation": updated.consolidation.model_dump(mode="json"),
        "max_concurrent_syncs": updated.max_concurrent_syncs,
        "updated_at": updated.updated_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------


@router.get("/policies/presets")
async def get_presets() -> list[dict]:
    """Return all available policy presets."""
    return list_presets()


# ---------------------------------------------------------------------------
# Channel policy CRUD
# ---------------------------------------------------------------------------


@router.get("/channels/{channel_id}/policy")
async def get_channel_policy(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> dict:
    """Return the channel policy (raw + effective resolved)."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    policy = await stores.mongodb.get_channel_policy(channel_id)
    defaults = await stores.mongodb.get_global_defaults()

    if policy is None:
        effective = resolve_policy(None, defaults)
        return {
            "channel_id": channel_id,
            "preset": None,
            "policy": None,
            "effective": {
                "sync": effective.sync.model_dump(mode="json"),
                "ingestion": effective.ingestion.model_dump(mode="json"),
                "consolidation": effective.consolidation.model_dump(mode="json"),
            },
            "enabled": True,
            "syncs_since_last_consolidation": 0,
            "created_at": None,
            "updated_at": None,
        }

    return await _policy_response(channel_id, policy)


@router.put("/channels/{channel_id}/policy")
async def upsert_channel_policy(
    channel_id: str,
    body: PolicyUpdateRequest,
    principal: Principal = Depends(require_user),
) -> dict:
    """Create or update a channel policy."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()

    # If applying a preset, populate from preset config
    if body.preset and body.preset != "custom":
        preset_cfg = get_preset_config(body.preset)
        if preset_cfg is None:
            raise HTTPException(status_code=404, detail=f"Preset '{body.preset}' not found")
        sync_cfg = preset_cfg["sync"]
        ingestion_cfg = preset_cfg["ingestion"]
        consolidation_cfg = preset_cfg["consolidation"]
    else:
        sync_cfg = body.sync or SyncConfig()
        ingestion_cfg = body.ingestion or IngestionConfig()
        consolidation_cfg = body.consolidation or ConsolidationConfig()

    _validate_policy_crons(sync_cfg, consolidation_cfg)

    existing = await stores.mongodb.get_channel_policy(channel_id)
    if existing:
        existing.sync = sync_cfg
        existing.ingestion = ingestion_cfg
        existing.consolidation = consolidation_cfg
        existing.preset = body.preset or "custom"
        if body.enabled is not None:
            existing.enabled = body.enabled
        policy = await stores.mongodb.upsert_channel_policy(existing)
    else:
        policy = ChannelPolicy(
            channel_id=channel_id,
            preset=body.preset or "custom",
            sync=sync_cfg,
            ingestion=ingestion_cfg,
            consolidation=consolidation_cfg,
            enabled=body.enabled if body.enabled is not None else True,
        )
        policy = await stores.mongodb.upsert_channel_policy(policy)

    logger.info("Policies API: channel=%s policy upserted preset=%s", channel_id, policy.preset)

    # Notify scheduler if available
    try:
        from beever_atlas.services.scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler:
            await scheduler.on_policy_changed(channel_id)
    except ImportError:
        pass

    return await _policy_response(channel_id, policy)


@router.delete("/channels/{channel_id}/policy")
async def delete_channel_policy(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> dict:
    """Delete a channel policy, reverting to global defaults."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    deleted = await stores.mongodb.delete_channel_policy(channel_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="No policy found for this channel")

    logger.info("Policies API: channel=%s policy deleted", channel_id)

    # Notify scheduler
    try:
        from beever_atlas.services.scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler:
            await scheduler.on_policy_changed(channel_id)
    except ImportError:
        pass

    return {"status": "deleted", "channel_id": channel_id}


@router.get("/policies")
async def list_policies() -> list[dict]:
    """List all channel policies with effective values."""
    stores = get_stores()
    policies = await stores.mongodb.list_channel_policies()
    defaults = await stores.mongodb.get_global_defaults()

    result = []
    for policy in policies:
        effective = resolve_policy(policy, defaults)
        result.append({
            "channel_id": policy.channel_id,
            "preset": policy.preset,
            "enabled": policy.enabled,
            "trigger_mode": effective.sync.trigger_mode,
            "interval_minutes": effective.sync.interval_minutes,
            "consolidation_strategy": effective.consolidation.strategy,
            "syncs_since_last_consolidation": policy.syncs_since_last_consolidation,
            "updated_at": policy.updated_at.isoformat(),
        })
    return result


@router.post("/channels/bulk-policy")
async def bulk_apply_policy(
    body: BulkPolicyRequest,
    principal: Principal = Depends(require_user),
) -> dict:
    """Apply a preset to multiple channels at once."""
    preset_cfg = get_preset_config(body.preset)
    if preset_cfg is None:
        raise HTTPException(status_code=404, detail=f"Preset '{body.preset}' not found")

    # RES-177 H1: verify ownership for EVERY channel BEFORE any mutation.
    # If any channel is not owned by the caller, the whole bulk fails
    # and no policy is written — avoids partial-application of a preset.
    for channel_id in body.channel_ids:
        await assert_channel_access(principal, channel_id)

    stores = get_stores()
    updated = []
    for channel_id in body.channel_ids:
        existing = await stores.mongodb.get_channel_policy(channel_id)
        if existing:
            existing.sync = preset_cfg["sync"]
            existing.ingestion = preset_cfg["ingestion"]
            existing.consolidation = preset_cfg["consolidation"]
            existing.preset = body.preset
            await stores.mongodb.upsert_channel_policy(existing)
        else:
            policy = ChannelPolicy(
                channel_id=channel_id,
                preset=body.preset,
                sync=preset_cfg["sync"],
                ingestion=preset_cfg["ingestion"],
                consolidation=preset_cfg["consolidation"],
            )
            await stores.mongodb.upsert_channel_policy(policy)
        updated.append(channel_id)

    logger.info("Policies API: bulk applied preset=%s to %d channels", body.preset, len(updated))

    # Notify scheduler for each
    try:
        from beever_atlas.services.scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler:
            for channel_id in updated:
                await scheduler.on_policy_changed(channel_id)
    except ImportError:
        pass

    return {"preset": body.preset, "channels_updated": updated}
