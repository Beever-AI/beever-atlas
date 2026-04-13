"""Pipeline orchestrator — decides when to run consolidation based on channel policy."""

from __future__ import annotations

import asyncio
import logging

from beever_atlas.models.sync_policy import ConsolidationStrategy
from beever_atlas.services.policy_resolver import resolve_effective_policy
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

# Track running consolidation tasks to avoid duplicates
_consolidation_tasks: dict[str, asyncio.Task] = {}
# Per-channel locks guarding the read+set of ``_consolidation_tasks``.
# Without these, two near-simultaneous ``on_ingestion_complete`` calls for
# the same channel can both see "no task running" and spawn duplicates.
_consolidation_locks: dict[str, asyncio.Lock] = {}
_consolidation_locks_guard = asyncio.Lock()


async def _get_lock(channel_id: str) -> asyncio.Lock:
    async with _consolidation_locks_guard:
        lock = _consolidation_locks.get(channel_id)
        if lock is None:
            lock = asyncio.Lock()
            _consolidation_locks[channel_id] = lock
        return lock


async def on_ingestion_complete(channel_id: str, facts_created: int) -> None:
    """Called by SyncRunner after ingestion finishes.

    Decides whether to trigger consolidation based on the channel's policy.
    """
    policy = await resolve_effective_policy(channel_id)
    strategy = policy.consolidation.strategy

    match strategy:
        case ConsolidationStrategy.AFTER_EVERY_SYNC:
            logger.info(
                "Orchestrator: triggering consolidation (after_every_sync) channel=%s facts=%d",
                channel_id, facts_created,
            )
            await _spawn_consolidation(channel_id)

        case ConsolidationStrategy.AFTER_N_SYNCS:
            stores = get_stores()
            count = await stores.mongodb.increment_sync_counter(channel_id)
            threshold = policy.consolidation.after_n_syncs or 3
            logger.info(
                "Orchestrator: after_n_syncs channel=%s count=%d/%d",
                channel_id, count, threshold,
            )
            if count >= threshold:
                await _spawn_consolidation(channel_id)
                # Counter reset happens in _run_consolidation after completion

        case ConsolidationStrategy.SCHEDULED:
            logger.info(
                "Orchestrator: skipping consolidation (scheduled independently) channel=%s",
                channel_id,
            )

        case ConsolidationStrategy.MANUAL:
            logger.info(
                "Orchestrator: skipping consolidation (manual) channel=%s",
                channel_id,
            )

        case _:
            logger.warning(
                "Orchestrator: unknown strategy %s for channel=%s, skipping",
                strategy, channel_id,
            )


async def _spawn_consolidation(channel_id: str) -> None:
    """Spawn a consolidation task if one isn't already running.

    The read-then-set of ``_consolidation_tasks[channel_id]`` is serialized
    via a per-channel ``asyncio.Lock`` so concurrent callers can't both
    observe "no running task" and each create one.
    """
    lock = await _get_lock(channel_id)
    async with lock:
        existing = _consolidation_tasks.get(channel_id)
        if existing and not existing.done():
            logger.info(
                "Orchestrator: consolidation already running for channel=%s, skipping",
                channel_id,
            )
            return

        task = asyncio.create_task(_run_consolidation(channel_id))
        _consolidation_tasks[channel_id] = task


async def _run_consolidation(channel_id: str) -> None:
    """Execute consolidation. Errors are logged, never propagated."""
    try:
        from beever_atlas.infra.config import get_settings
        from beever_atlas.services.consolidation import ConsolidationService

        stores = get_stores()
        settings = get_settings()
        effective = await resolve_effective_policy(channel_id)
        service = ConsolidationService(
            stores.weaviate, settings, graph=stores.graph,
            consolidation_config=effective.consolidation,
        )
        # Resolve display name so ChannelSummary is written with a human-
        # readable heading, not the raw channel_id.
        channel_name = await stores.mongodb.get_channel_display_name(channel_id) or ""
        result = await service.on_sync_complete(channel_id, channel_name=channel_name)

        # Reset sync counter after successful consolidation
        await stores.mongodb.reset_sync_counter(channel_id)

        logger.info(
            "Orchestrator: consolidation complete channel=%s created=%d updated=%d facts=%d",
            channel_id, result.clusters_created, result.clusters_updated, result.facts_clustered,
        )

        # Persist consolidation result as activity event
        await stores.mongodb.log_activity(
            event_type="consolidation_completed",
            channel_id=channel_id,
            details={
                "clusters_created": result.clusters_created,
                "clusters_updated": result.clusters_updated,
                "clusters_merged": result.clusters_merged,
                "clusters_deleted": result.clusters_deleted,
                "facts_clustered": result.facts_clustered,
                "summaries_generated": result.summaries_generated,
                "errors": result.errors,
            },
        )
    except Exception as exc:
        logger.error(
            "Orchestrator: consolidation failed channel=%s: %s",
            channel_id, exc, exc_info=True,
        )
        # Persist failure as activity event (best effort)
        try:
            await stores.mongodb.log_activity(
                event_type="consolidation_failed",
                channel_id=channel_id,
                details={"error": str(exc)},
            )
        except Exception:
            pass
    finally:
        _consolidation_tasks.pop(channel_id, None)


async def trigger_consolidation(channel_id: str) -> None:
    """Manually trigger consolidation regardless of policy. Used by API."""
    logger.info("Orchestrator: manual consolidation triggered channel=%s", channel_id)
    await _spawn_consolidation(channel_id)


def get_active_consolidation_tasks() -> dict[str, asyncio.Task]:
    """Return the active consolidation tasks dict (for shutdown)."""
    return _consolidation_tasks
