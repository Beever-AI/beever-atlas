"""Pipeline orchestrator — decides when to run consolidation based on channel policy."""

from __future__ import annotations

import asyncio
import logging

from beever_atlas.models.sync_policy import ConsolidationStrategy
from beever_atlas.services.policy_resolver import resolve_effective_policy
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)

# Track running consolidation tasks to avoid duplicates.
# asyncio is single-threaded within one event loop, so the
# read-then-set of ``_consolidation_tasks[channel_id]`` in
# ``_spawn_consolidation`` is atomic: no ``await`` between the
# membership check and the ``asyncio.create_task`` assignment.
_consolidation_tasks: dict[str, asyncio.Task] = {}

# Channels that received at least one per-batch consolidation pass during
# the current sync and therefore owe a ``summarize_settled`` run on the
# next ``memory_settled`` event. Membership is added in ``_run_consolidation``
# (decoupled-summary mode) and removed in ``summarize_settled_for_channel``
# after the LLM batch completes. A set is fine: ``memory_settled`` for a
# channel that never had a per-batch consolidation is a no-op.
_channels_pending_summary: set[str] = set()

# delete-channel-v2 Wave 0 — purge guard for the SYNC ``_spawn_consolidation``.
# ``_spawn_consolidation`` is intentionally synchronous (the read-then-set of
# ``_consolidation_tasks`` must be await-free to stay atomic in one event
# loop), so it cannot ``await is_purging``. Chosen approach: a process-local
# cached snapshot of purging channel_ids, refreshed by ``_refresh_purging_snapshot``
# which the async callers (``on_ingestion_complete``, ``consolidate_only``,
# ``trigger_consolidation``) await immediately before spawning. The sync
# function then does a cheap membership check against the snapshot. This keeps
# the spawn await-free while still gating it; the snapshot is at most one
# call-stale, which is acceptable because the durable purge lock + reconciler
# per-fact filter (Wave 0) are the real convergence guarantees.
_purging_snapshot: set[str] = set()


async def _refresh_purging_snapshot() -> None:
    """Refresh the cached purging-channel snapshot read by the sync
    ``_spawn_consolidation`` guard. Best-effort — a Mongo blip leaves the
    prior snapshot in place rather than crashing the caller."""
    global _purging_snapshot
    try:
        _purging_snapshot = await get_stores().mongodb.get_purging_channel_ids()
    except Exception:  # noqa: BLE001 — guard refresh must not break the pipeline
        logger.exception("Orchestrator: failed to refresh purging snapshot")


async def on_ingestion_complete(channel_id: str, facts_created: int) -> None:
    """Called by SyncRunner after ingestion finishes.

    Decides whether to trigger consolidation based on the channel's policy.
    """
    # delete-channel-v2 Wave 0 — refresh the purge snapshot the sync
    # ``_spawn_consolidation`` guard reads.
    await _refresh_purging_snapshot()
    policy = await resolve_effective_policy(channel_id)
    strategy = policy.consolidation.strategy

    match strategy:
        case ConsolidationStrategy.AFTER_EVERY_SYNC:
            logger.info(
                "Orchestrator: triggering consolidation (after_every_sync) channel=%s facts=%d",
                channel_id,
                facts_created,
            )
            _spawn_consolidation(channel_id)

        case ConsolidationStrategy.AFTER_N_SYNCS:
            stores = get_stores()
            count = await stores.mongodb.increment_sync_counter(channel_id)
            threshold = policy.consolidation.after_n_syncs or 3
            logger.info(
                "Orchestrator: after_n_syncs channel=%s count=%d/%d",
                channel_id,
                count,
                threshold,
            )
            if count >= threshold:
                _spawn_consolidation(channel_id)
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
                strategy,
                channel_id,
            )


def _spawn_consolidation(channel_id: str) -> None:
    """Spawn a consolidation task if one isn't already running.

    Synchronous by design: the read-then-set of
    ``_consolidation_tasks[channel_id]`` runs with no ``await`` in
    between, so within a single event loop it is atomic and concurrent
    callers cannot both observe "no running task" and each create one.

    delete-channel-v2 Wave 0 — purge guard. Reads the cached snapshot
    (refreshed by the async callers via ``_refresh_purging_snapshot``) so
    the function stays await-free; skips spawning for a purging channel.
    """
    if channel_id in _purging_snapshot:
        logger.info(
            "Orchestrator: skipping consolidation — channel is purging channel=%s",
            channel_id,
        )
        return
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
    """Execute consolidation. Errors are logged, never propagated.

    Path selection depends on ``CONSOLIDATION_SUMMARIZE_ON_SETTLE``:

    * True (default) — runs ``assign_clusters_only`` ONLY. The expensive
      LLM summarization is deferred to :func:`summarize_settled_for_channel`,
      which fires on ``memory_settled`` once per channel per sync.
    * False — legacy path: runs ``on_sync_complete`` (clustering + LLM
      summaries together) per batch.
    """
    try:
        from beever_atlas.infra.config import get_settings
        from beever_atlas.services.consolidation import ConsolidationService

        stores = get_stores()
        settings = get_settings()
        effective = await resolve_effective_policy(channel_id)
        service = ConsolidationService(
            stores.weaviate,
            settings,
            graph=stores.graph,
            consolidation_config=effective.consolidation,
        )
        # Resolve display name so ChannelSummary is written with a human-
        # readable heading, not the raw channel_id.
        channel_name = await stores.mongodb.get_channel_display_name(channel_id) or ""
        if getattr(settings, "consolidation_summarize_on_settle", True):
            # CRITICAL: mark BEFORE the await so memory_settled subscribers
            # firing concurrently with this task still see the gate flipped.
            # Previously the add happened AFTER assign_clusters_only completed
            # (~500ms-2s), which lost the race against memory_settled — the
            # AutoOverviewSubscriber timed out waiting for a channel_summary
            # that summarize_settled never wrote because the gate looked empty.
            # `summarize_settled_for_channel` awaits this consolidation task
            # in-flight to ensure it reads fresh cluster state.
            _channels_pending_summary.add(channel_id)
            result = await service.assign_clusters_only(channel_id)
        else:
            result = await service.on_sync_complete(channel_id, channel_name=channel_name)

        # Reset sync counter after successful consolidation
        await stores.mongodb.reset_sync_counter(channel_id)

        logger.info(
            "Orchestrator: consolidation complete channel=%s created=%d updated=%d facts=%d",
            channel_id,
            result.clusters_created,
            result.clusters_updated,
            result.facts_clustered,
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
            channel_id,
            exc,
            exc_info=True,
        )
        # Persist failure as activity event (best effort)
        try:
            await get_stores().mongodb.log_activity(
                event_type="consolidation_failed",
                channel_id=channel_id,
                details={"error": str(exc)},
            )
        except Exception:
            pass
    finally:
        _consolidation_tasks.pop(channel_id, None)


async def consolidate_only(channel_id: str) -> None:
    """Trigger consolidation for *channel_id* WITHOUT incrementing the sync counter.

    Called by the ExtractionWorker subscriber in server/app.py so that each
    completed batch fires a consolidation without ticking the AFTER_N_SYNCS
    counter N times.  The counter is incremented exactly once per logical sync
    in SyncRunner._run_sync (legacy inline path) and is NOT touched here.

    Policy gate: only spawns for AFTER_EVERY_SYNC and AFTER_N_SYNCS strategies
    (the same strategies the subscriber already checks before calling us).
    """
    logger.info("Orchestrator: consolidate_only (no counter) channel=%s", channel_id)
    # delete-channel-v2 Wave 0 — refresh the purge snapshot before spawning.
    await _refresh_purging_snapshot()
    # Race fix: set the summarize-settled gate SYNCHRONOUSLY before spawning
    # the consolidation task. The previous add inside ``_run_consolidation``
    # only flipped the gate after the event loop scheduled the task, which
    # lost the race against ``memory_settled`` subscribers firing in the
    # same tick — ``summarize_settled_for_channel`` saw an empty gate, took
    # the silent early-return, and ``channel_summary`` was never written.
    # AutoOverview then polled 60s for the missing summary and raised
    # WikiNotReadyError, leaving the wiki permanently 404. No await sits
    # between this line and the task spawn, so the window is closed.
    try:
        from beever_atlas.infra.config import get_settings

        if get_settings().consolidation_summarize_on_settle:
            _channels_pending_summary.add(channel_id)
    except Exception:  # noqa: BLE001 — gate is belt-and-braces; the in-task add still runs
        pass
    _spawn_consolidation(channel_id)


async def trigger_consolidation(channel_id: str) -> None:
    """Manually trigger consolidation regardless of policy. Used by API."""
    logger.info("Orchestrator: manual consolidation triggered channel=%s", channel_id)
    # delete-channel-v2 Wave 0 — refresh the purge snapshot before spawning.
    await _refresh_purging_snapshot()
    _spawn_consolidation(channel_id)


def get_active_consolidation_tasks() -> dict[str, asyncio.Task]:
    """Return the active consolidation tasks dict (for shutdown)."""
    return _consolidation_tasks


async def cancel_consolidation(channel_id: str) -> bool:
    """Cancel the in-flight consolidation task for ``channel_id`` (process-local).

    Analogous to :meth:`SyncRunner.cancel_sync` — best-effort same-process
    cleanup used by the Wave-2 purge service before the durable lock + guards
    take over. Cancels the task, awaits it, swallows ``CancelledError``, pops
    it from the registry.

    Returns:
        True if a live task was cancelled, False if none was active.

    EE caveat: only cancels tasks in THIS process; cross-worker stops rely on
    the purge lock + the ``_spawn_consolidation`` guard.
    """
    task = _consolidation_tasks.get(channel_id)
    if task is None or task.done():
        _consolidation_tasks.pop(channel_id, None)
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception as exc:  # noqa: BLE001 — log, don't propagate
        logger.warning(
            "Orchestrator: task raised during cancel_consolidation channel=%s: %s",
            channel_id,
            exc,
        )
    _consolidation_tasks.pop(channel_id, None)
    return True


async def summarize_settled_for_channel(channel_id: str) -> None:
    """Run the deferred summary pass for *channel_id* on ``memory_settled``.

    Gated by ``_channels_pending_summary`` so memory_settled events for
    channels that never saw a per-batch consolidation (e.g. SCHEDULED
    strategy, or a channel that had zero new facts) are cheap no-ops.

    Always awaits the consolidation LLM batch in-band so the maintainer's
    ``memory_settled`` subscriber — which fires its debounced flush after a
    5s ``settle_debounce_seconds`` window — sees the freshly written
    cluster/channel summaries when it rewrites pages.

    Idempotent: removes the channel from the pending set before running so
    a re-fire during the LLM batch will queue another pass; the
    ``summarize_settled`` method itself short-circuits when no clusters are
    dirty.

    Errors are logged, never propagated — the maintainer's flush is
    independent and must run regardless.
    """
    # Pop-before-run so a concurrent memory_settled re-fire can enqueue a
    # follow-up. asyncio is single-threaded so the discard+create-task
    # boundary is atomic; no race.
    if channel_id not in _channels_pending_summary:
        return
    _channels_pending_summary.discard(channel_id)

    # If consolidation is still running for this channel (we added to the
    # pending-set BEFORE awaiting assign_clusters_only), wait for it to
    # finish so we read fresh cluster state. Otherwise summarize_settled
    # would race against the in-flight consolidation and see no clusters
    # to summarize.
    in_flight_consolidation = _consolidation_tasks.get(channel_id)
    if in_flight_consolidation is not None and not in_flight_consolidation.done():
        logger.info(
            "Orchestrator: summarize_settled awaiting in-flight consolidation channel=%s",
            channel_id,
        )
        try:
            await in_flight_consolidation
        except Exception:  # noqa: BLE001 — already logged in _run_consolidation
            pass

    try:
        from beever_atlas.infra.config import get_settings
        from beever_atlas.services.consolidation import ConsolidationService

        stores = get_stores()
        settings = get_settings()
        effective = await resolve_effective_policy(channel_id)
        service = ConsolidationService(
            stores.weaviate,
            settings,
            graph=stores.graph,
            consolidation_config=effective.consolidation,
        )
        channel_name = await stores.mongodb.get_channel_display_name(channel_id) or ""
        result = await service.summarize_settled(channel_id, channel_name=channel_name)
        logger.info(
            "Orchestrator: summarize_settled complete channel=%s summaries=%d errors=%d",
            channel_id,
            result.summaries_generated,
            len(result.errors),
        )
    except Exception as exc:
        logger.error(
            "Orchestrator: summarize_settled failed channel=%s: %s",
            channel_id,
            exc,
            exc_info=True,
        )
