"""Policy resolution — merges channel overrides with global defaults."""

from __future__ import annotations

import time

from pydantic import BaseModel

from beever_atlas.models.sync_policy import (
    ChannelPolicy,
    ConsolidationConfig,
    GlobalPolicyDefaults,
    IngestionConfig,
    ResolvedPolicy,
    SyncConfig,
    WikiConfig,
)
from beever_atlas.stores import get_stores

# In-process cache for global defaults (rarely changes)
_CACHE_TTL_SECONDS = 60
_cached_defaults: GlobalPolicyDefaults | None = None
_cached_at: float = 0.0


def _merge_config(channel_cfg: BaseModel, default_cfg: BaseModel) -> dict:
    """Merge two Pydantic model instances field-by-field.

    For each field, use the channel value if non-None, else the default value.
    """
    merged: dict = {}
    for field_name in type(default_cfg).model_fields:
        channel_val = getattr(channel_cfg, field_name)
        default_val = getattr(default_cfg, field_name)
        merged[field_name] = channel_val if channel_val is not None else default_val
    return merged


def resolve_policy(
    channel: ChannelPolicy | None,
    defaults: GlobalPolicyDefaults,
) -> ResolvedPolicy:
    """Merge a channel policy with global defaults field-by-field.

    Returns a fully-populated ResolvedPolicy with no null fields.
    If *channel* is None, returns global defaults directly.
    """
    if channel is None:
        return ResolvedPolicy(
            sync=defaults.sync,
            ingestion=defaults.ingestion,
            consolidation=defaults.consolidation,
            wiki=defaults.wiki,
        )

    return ResolvedPolicy(
        sync=SyncConfig(**_merge_config(channel.sync, defaults.sync)),
        ingestion=IngestionConfig(**_merge_config(channel.ingestion, defaults.ingestion)),
        consolidation=ConsolidationConfig(
            **_merge_config(channel.consolidation, defaults.consolidation)
        ),
        wiki=WikiConfig(**_merge_config(channel.wiki, defaults.wiki)),
    )


async def _get_cached_defaults() -> GlobalPolicyDefaults:
    """Return global defaults with 60s TTL cache to reduce MongoDB reads."""
    global _cached_defaults, _cached_at
    now = time.monotonic()
    if _cached_defaults is not None and (now - _cached_at) < _CACHE_TTL_SECONDS:
        return _cached_defaults
    stores = get_stores()
    _cached_defaults = await stores.mongodb.get_global_defaults()
    _cached_at = now
    return _cached_defaults


def invalidate_defaults_cache() -> None:
    """Force next call to re-read from MongoDB. Called after admin updates defaults."""
    global _cached_defaults, _cached_at
    _cached_defaults = None
    _cached_at = 0.0


async def resolve_effective_policy(channel_id: str) -> ResolvedPolicy:
    """Convenience: load channel policy + global defaults from stores and resolve."""
    stores = get_stores()
    channel_policy = await stores.mongodb.get_channel_policy(channel_id)
    global_defaults = await _get_cached_defaults()
    return resolve_policy(channel_policy, global_defaults)
