"""Channel ID → display name resolver with in-memory cache."""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Module-level cache: channel_id → channel_name
_channel_name_cache: dict[str, str] = {}


async def resolve_channel_name(channel_id: str) -> str:
    """Resolve a channel ID to its display name.

    Uses an in-memory cache to avoid repeated MongoDB lookups. Falls back to the
    raw channel_id when resolution fails — but ONLY caches SUCCESSFUL
    resolutions. A transient store error or a not-yet-synced channel must not
    poison the cache with the raw id: that would permanently disable name
    resolution for the channel until process restart, which silently breaks the
    cross-channel guard (it can't tell "this channel" from another when the name
    is a raw id) and channel-name rendering. Leaving misses uncached lets a
    later call resolve correctly once the store is ready / the channel syncs.
    """
    if channel_id in _channel_name_cache:
        return _channel_name_cache[channel_id]

    try:
        from beever_atlas.stores import get_stores

        store = get_stores().mongodb
        # Use the existing get_channel_display_name method which queries
        # activity_events for details.channel_name (the canonical source).
        name = await store.get_channel_display_name(channel_id)
        if name:
            _channel_name_cache[channel_id] = name  # cache real resolutions only
            return name
        # Not found yet (e.g. channel not synced) — return raw id but do NOT
        # cache, so a later call retries once the name exists.
        return channel_id
    except Exception:
        # Transient store error (e.g. Mongo blip under load) — return raw id but
        # do NOT poison the cache; retry on the next call.
        logger.debug("Could not resolve channel name for %s (will retry)", channel_id)
        return channel_id
