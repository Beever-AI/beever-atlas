"""Sync scheduler — manages scheduled ingestion and consolidation jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from apscheduler import AsyncScheduler
from apscheduler.datastores.memory import MemoryDataStore
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from beever_atlas.models.sync_policy import (
    ConsolidationStrategy,
    SyncTriggerMode,
)

logger = logging.getLogger(__name__)

_scheduler_instance: SyncScheduler | None = None


def init_scheduler(scheduler: SyncScheduler) -> None:
    global _scheduler_instance
    _scheduler_instance = scheduler


def get_scheduler() -> SyncScheduler | None:
    return _scheduler_instance


class SyncScheduler:
    """Manages scheduled sync and consolidation jobs via APScheduler 4.x."""

    def __init__(self, mongodb_uri: str) -> None:
        # ``mongodb_uri`` is preserved on the constructor signature for
        # backward compat with the lifespan call site, but the apscheduler
        # data store is now in-memory. Why: the previous MongoDB-backed
        # store tried to pickle bound-method callables (e.g.
        # ``self._extraction_tick``) which carry references to live
        # ``asyncio.Task`` objects — pickle can't serialize those, so EVERY
        # ``add_schedule`` raised SerializationError, the lifespan caught it
        # as "non-fatal", and the scheduler silently never registered any
        # jobs (extraction worker never ticked, decoupled-extraction path
        # was completely broken on every fresh boot).
        # MemoryDataStore is the apscheduler default and is appropriate
        # for OSS solo deploys: schedules are just periodic timers that
        # re-register on every boot from ``startup()``; no replica
        # coordination is needed. The actual queue state lives in
        # ``channel_messages`` (Mongo), not in the scheduler.
        self._mongodb_uri = mongodb_uri
        # SECURITY — CVE-2026-31072 / GHSA-9cfw-f3f9-7mm7 (apscheduler RCE).
        # apscheduler's serializing data stores (SQLAlchemyDataStore /
        # MongoDBDataStore) and serializing event brokers (e.g. RedisEventBroker /
        # MQTTEventBroker) reconstruct schedules and events by deserializing
        # stored task state via JSONSerializer / CBORSerializer. A crafted
        # payload in the backing store can be deserialized into arbitrary object
        # construction → remote code execution. There is currently NO patched
        # apscheduler release that closes this; the only safe posture is to
        # avoid the serializer code path entirely.
        # MemoryDataStore never serializes — schedules live as in-process Python
        # objects and are re-registered from ``startup()`` on every boot — so the
        # vulnerable JSONSerializer / CBORSerializer path is unreachable here.
        # DO NOT replace MemoryDataStore with a persistent serializing data store
        # (SQLAlchemyDataStore / MongoDBDataStore) or switch to a serializing
        # event broker (e.g. RedisEventBroker) until apscheduler ships a patched
        # release — doing so re-introduces the deserialization RCE. (See also the
        # bound-method pickling rationale above; that switch would also break job
        # registration.)
        self._data_store = MemoryDataStore()
        self._scheduler = AsyncScheduler(data_store=self._data_store)
        self._global_semaphore: asyncio.Semaphore | None = None
        self._started = False

    async def startup(self) -> None:
        """Load all channel policies, register jobs, start the scheduler.

        Issue #36 — `await wait_for_stores_ready()` is a no-op in the
        current lifespan ordering (the event is set before this method
        runs) but documents the dependency and protects against future
        lifespan reorders.
        """
        from beever_atlas.stores import get_stores, wait_for_stores_ready

        await wait_for_stores_ready()
        stores = get_stores()
        defaults = await stores.mongodb.get_global_defaults()
        self._global_semaphore = asyncio.Semaphore(defaults.max_concurrent_syncs)

        # Newer apscheduler requires the scheduler's async context to be
        # entered BEFORE ``add_schedule`` is called — otherwise the calls
        # raise "The scheduler has not been initialized yet". Order is:
        #   1. enter ``__aenter__()`` so the data store + locks are live
        #   2. register all schedules (sync, consolidation, extraction
        #      worker tick/sweep)
        # Earlier code did this in the opposite order and the entire
        # scheduler startup silently failed via the non-fatal try/except
        # in ``server/app.py`` lifespan, leaving the ExtractionWorker
        # un-ticked and the redesign decoupled-extraction path broken.
        await self._scheduler.__aenter__()

        # Load all channel policies and register jobs
        policies = await stores.mongodb.list_channel_policies()
        for policy in policies:
            if not policy.enabled:
                continue
            await self._register_sync_job(policy.channel_id, policy)
            await self._register_consolidation_job(policy.channel_id, policy)

        # Register the background ExtractionWorker. Two periodic jobs:
        # ``tick`` drains the pending queue, ``sweep_stale`` recovers rows
        # stuck in "extracting" past the stale window. The worker singleton
        # is registered so the WikiMaintainer (and admin endpoints) can
        # subscribe to ``on_extraction_done`` without a constructor weave.
        await self._register_extraction_worker_jobs()

        # delete-channel-v2 Wave 0 — channel hard-purge reaper. Re-invokes the
        # purge for stale ``channel_purge_locks`` rows (crashed / partial
        # purges). Flag-disable-able for processes that don't run the
        # scheduler. The Wave-2 purge service plugs into the seam in
        # ``_purge_reaper_tick``.
        await self._register_purge_reaper_job()

        # apscheduler 4 — ``__aenter__`` initialises the data store + locks,
        # but does NOT start the job-runner loop. Without this call, schedules
        # are stored but never fire. The previous codebase didn't call it
        # because the older MongoDBDataStore variant did so internally; the
        # MemoryDataStore swap exposed the gap.
        await self._scheduler.start_in_background()

        self._started = True
        logger.info(
            "SyncScheduler: started with %d channel policies, max_concurrent_syncs=%d",
            len(policies),
            defaults.max_concurrent_syncs,
        )

    async def shutdown(self) -> None:
        """Stop the scheduler gracefully."""
        if self._started:
            await self._scheduler.__aexit__(None, None, None)
            self._started = False
            logger.info("SyncScheduler: shutdown complete")

    async def on_policy_changed(self, channel_id: str) -> None:
        """Called when a channel policy is created/updated/deleted.

        Re-registers or removes scheduler jobs for this channel.
        """
        if not self._started:
            return

        from beever_atlas.stores import get_stores

        stores = get_stores()
        policy = await stores.mongodb.get_channel_policy(channel_id)

        # Remove existing jobs for this channel
        await self._remove_jobs(channel_id)

        # Re-register if policy exists and is enabled
        if policy and policy.enabled:
            await self._register_sync_job(channel_id, policy)
            await self._register_consolidation_job(channel_id, policy)
            logger.info("SyncScheduler: re-registered jobs for channel=%s", channel_id)
        else:
            logger.info("SyncScheduler: removed jobs for channel=%s", channel_id)

    async def deregister_channel_jobs(self, channel_id: str) -> None:
        """Unconditionally remove ``sync:{id}`` + ``consolidate:{id}`` timers.

        delete-channel-v2 Wave 0 thin wrapper for the Wave-2 purge service.
        Unlike :meth:`on_policy_changed` (which RE-registers when a policy
        still exists), this only removes — so the service can de-register
        deterministically without depending on policy-delete ordering. Safe
        no-op when the scheduler hasn't started.
        """
        if not self._started:
            return
        await self._remove_jobs(channel_id)
        logger.info("SyncScheduler: de-registered jobs for channel=%s (purge)", channel_id)

    async def acquire_sync_semaphore(self) -> None:
        """Acquire the global concurrency semaphore (for manual syncs too)."""
        if self._global_semaphore:
            await self._global_semaphore.acquire()

    def release_sync_semaphore(self) -> None:
        """Release the global concurrency semaphore."""
        if self._global_semaphore:
            self._global_semaphore.release()

    # ------------------------------------------------------------------
    # Internal: job registration
    # ------------------------------------------------------------------

    async def _register_sync_job(self, channel_id: str, policy) -> None:
        """Register an APScheduler job for sync based on the policy's trigger mode."""
        from beever_atlas.services.policy_resolver import resolve_policy
        from beever_atlas.stores import get_stores

        stores = get_stores()
        defaults = await stores.mongodb.get_global_defaults()
        effective = resolve_policy(policy, defaults)

        trigger_mode = effective.sync.trigger_mode
        if trigger_mode == SyncTriggerMode.INTERVAL and effective.sync.interval_minutes:
            trigger = IntervalTrigger(minutes=effective.sync.interval_minutes)
            await self._scheduler.add_schedule(
                self._execute_sync,
                trigger,
                id=f"sync:{channel_id}",
                args=[channel_id],
                conflict_policy="do_nothing",
            )
            logger.info(
                "SyncScheduler: registered interval sync channel=%s every=%dmin",
                channel_id,
                effective.sync.interval_minutes,
            )
        elif trigger_mode == SyncTriggerMode.CRON and effective.sync.cron_expression:
            parts = effective.sync.cron_expression.split()
            if len(parts) == 5:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                )
                await self._scheduler.add_schedule(
                    self._execute_sync,
                    trigger,
                    id=f"sync:{channel_id}",
                    args=[channel_id],
                    conflict_policy="do_nothing",
                )
                logger.info(
                    "SyncScheduler: registered cron sync channel=%s cron=%s",
                    channel_id,
                    effective.sync.cron_expression,
                )

    async def _register_consolidation_job(self, channel_id: str, policy) -> None:
        """Register a separate consolidation job if strategy=scheduled."""
        from beever_atlas.services.policy_resolver import resolve_policy
        from beever_atlas.stores import get_stores

        stores = get_stores()
        defaults = await stores.mongodb.get_global_defaults()
        effective = resolve_policy(policy, defaults)

        if (
            effective.consolidation.strategy == ConsolidationStrategy.SCHEDULED
            and effective.consolidation.cron_expression
        ):
            parts = effective.consolidation.cron_expression.split()
            if len(parts) == 5:
                trigger = CronTrigger(
                    minute=parts[0],
                    hour=parts[1],
                    day=parts[2],
                    month=parts[3],
                    day_of_week=parts[4],
                )
                await self._scheduler.add_schedule(
                    self._execute_consolidation,
                    trigger,
                    id=f"consolidate:{channel_id}",
                    args=[channel_id],
                    conflict_policy="do_nothing",
                )
                logger.info(
                    "SyncScheduler: registered cron consolidation channel=%s cron=%s",
                    channel_id,
                    effective.consolidation.cron_expression,
                )

    async def _remove_jobs(self, channel_id: str) -> None:
        """Remove all scheduler jobs for a channel."""
        for job_id in [f"sync:{channel_id}", f"consolidate:{channel_id}"]:
            try:
                await self._scheduler.remove_schedule(job_id)
            except Exception:
                pass  # Job may not exist

    async def _register_extraction_worker_jobs(self) -> None:
        """Register the global ExtractionWorker tick + sweep jobs.

        The worker is process-wide (one instance, not per-channel) — its
        atomic ``find_one_and_update`` claim is the safety primitive
        that lets multiple worker replicas (future) drain the same
        queue without double-processing. ``tick`` runs every
        ``_TICK_SECONDS`` (30s), ``sweep_stale`` every half the
        ``_STALE_SECONDS`` window. Both jobs short-circuit when there's
        no work, so the Mongo cost when the queue is idle is one cheap
        index probe per tick.

        Tuning constants (`_TICK_SECONDS`, `_STALE_SECONDS`) live in
        ``services/extraction_worker.py``, not in env vars — capacity
        planning decisions should be tracked as code changes, not via
        operator `.env` overrides.
        """
        from beever_atlas.services.extraction_worker import (
            _STALE_SECONDS,
            _TICK_SECONDS,
            ExtractionWorker,
            init_extraction_worker,
        )

        worker = ExtractionWorker(stale_seconds=_STALE_SECONDS)
        init_extraction_worker(worker)

        await self._scheduler.add_schedule(
            self._extraction_tick,
            IntervalTrigger(seconds=_TICK_SECONDS),
            id="extraction:tick",
            conflict_policy="do_nothing",
        )
        # Sweep interval is derived from the stale window so they stay
        # in proportion. Floor at 60s so we don't hammer Mongo on a
        # mis-configured tiny stale window.
        sweep_interval = max(60, _STALE_SECONDS // 2)
        await self._scheduler.add_schedule(
            self._extraction_sweep,
            IntervalTrigger(seconds=sweep_interval),
            id="extraction:sweep",
            conflict_policy="do_nothing",
        )
        logger.info(
            "SyncScheduler: ExtractionWorker registered tick=%ds sweep=%ds stale=%ds",
            _TICK_SECONDS,
            sweep_interval,
            _STALE_SECONDS,
        )

    async def _extraction_tick(self) -> None:
        """Scheduler entry point — fans through to the worker singleton."""
        from beever_atlas.services.extraction_worker import get_extraction_worker

        worker = get_extraction_worker()
        if worker is None:
            return
        try:
            await worker.tick()
        except Exception as exc:
            logger.error(
                "SyncScheduler: extraction tick failed: %s",
                exc,
                exc_info=True,
            )

    async def _extraction_sweep(self) -> None:
        """Periodic stale-extracting recovery sweep."""
        from beever_atlas.services.extraction_worker import get_extraction_worker

        worker = get_extraction_worker()
        if worker is None:
            return
        try:
            await worker.sweep_stale()
        except Exception as exc:
            logger.error(
                "SyncScheduler: extraction sweep failed: %s",
                exc,
                exc_info=True,
            )

    async def _register_purge_reaper_job(self) -> None:
        """Register the channel hard-purge reaper periodic job.

        delete-channel-v2 Wave 0. Scans ``channel_purge_locks`` for stale
        rows (purges that crashed mid-run) and re-invokes the purge for each
        so a partial purge converges with no user re-click. Flag-disabled via
        ``CHANNEL_PURGE_REAPER_ENABLED`` for processes that don't run the
        scheduler. Pattern mirrors ``_register_extraction_worker_jobs``.
        """
        from beever_atlas.infra.config import get_settings

        settings = get_settings()
        if not settings.channel_purge_reaper_enabled:
            logger.info("SyncScheduler: purge reaper disabled by config")
            return
        interval = max(30, int(settings.channel_purge_reaper_interval_s))
        # NOTE: do NOT call ``self._scheduler.configure_task(self._purge_reaper_tick,
        # max_running_jobs=1)`` here. APScheduler v4's ``configure_task`` serialises
        # the callable via ``callable_to_ref``, which raises ``SerializationError``
        # on a bound instance method — that exception aborts ``startup()`` before
        # ``start_in_background()`` runs, silently disabling ALL background jobs
        # (the ExtractionWorker tick included → extraction stuck at 0/N pending).
        # ``add_schedule`` stores the bound method directly (no serialisation),
        # matching every other job here. An overlap cap is unnecessary:
        # ``_purge_reaper_tick`` re-invokes CAS-idempotent purges (a still-running
        # purge returns ``already_in_progress``), so concurrent ticks are safe.
        await self._scheduler.add_schedule(
            self._purge_reaper_tick,
            IntervalTrigger(seconds=interval),
            id="purge:reaper",
            conflict_policy="do_nothing",
        )
        logger.info(
            "SyncScheduler: purge reaper registered interval=%ds threshold=%.0fs",
            interval,
            settings.channel_purge_reaper_threshold_s,
        )

    async def _purge_reaper_tick(self) -> None:
        """Re-invoke the purge for every stale ``channel_purge_locks`` row.

        Seam for Wave 2: the actual fan-out lives in
        ``services.channel_deletion.purge_channel`` which does NOT exist yet.
        We import it lazily inside the job and guard with try/except so this
        Wave-0 reaper is structurally complete and harmless until the Wave-2
        service lands — at which point this loop starts converging partial
        purges with zero further wiring. ``purge_channel`` re-claims via CAS
        (idempotent / re-entrant), so re-invoking a still-running purge is
        safe (the loser returns ``already_in_progress``).
        """
        from beever_atlas.infra.config import get_settings
        from beever_atlas.stores import get_stores

        settings = get_settings()
        stores = get_stores()
        try:
            stale = await stores.mongodb.list_stale_purge_locks(
                older_than_s=settings.channel_purge_reaper_threshold_s
            )
        except Exception as exc:
            logger.error(
                "SyncScheduler: purge reaper failed to list stale locks: %s",
                exc,
                exc_info=True,
            )
            return
        if not stale:
            return
        # Lazy import — keeps the ``services → services`` dependency local and
        # avoids importing the deletion fan-out at scheduler module load.
        from beever_atlas.services.channel_deletion import purge_channel

        for channel_id in stale:
            try:
                await purge_channel(channel_id, principal_id="reaper")
                logger.info(
                    "SyncScheduler: purge reaper re-invoked purge channel=%s",
                    channel_id,
                )
            except Exception as exc:
                logger.error(
                    "SyncScheduler: purge reaper re-invoke failed channel=%s: %s",
                    channel_id,
                    exc,
                    exc_info=True,
                )

    # ------------------------------------------------------------------
    # Job execution
    # ------------------------------------------------------------------

    async def _execute_sync(self, channel_id: str) -> None:
        """Execute a scheduled sync for a channel."""
        from beever_atlas.services.policy_resolver import resolve_effective_policy
        from beever_atlas.stores import get_stores

        try:
            # Check cooldown
            stores = get_stores()
            # delete-channel-v2 Wave 0 — abort a scheduled sync for a purging
            # channel. The timer may still fire between policy-delete and
            # de-register (or in a stale-lock window); bail out here so the
            # wall-clock scheduler can't resurrect a channel being torn down.
            if await stores.mongodb.is_purging(channel_id):
                logger.info(
                    "SyncScheduler: skipping scheduled sync — channel is purging channel=%s",
                    channel_id,
                )
                return
            effective = await resolve_effective_policy(channel_id)
            cooldown = effective.sync.min_sync_interval_minutes or 0

            if cooldown > 0:
                last_job = await stores.mongodb.get_sync_status(channel_id)
                if last_job and last_job.completed_at:
                    completed = last_job.completed_at
                    if completed.tzinfo is None:
                        completed = completed.replace(tzinfo=UTC)
                    elapsed = datetime.now(tz=UTC) - completed
                    if elapsed < timedelta(minutes=cooldown):
                        logger.info(
                            "SyncScheduler: skipping sync channel=%s (cooldown: %s < %dmin)",
                            channel_id,
                            elapsed,
                            cooldown,
                        )
                        return

            # Acquire semaphore. Track acquisition so a wait_for timeout
            # (which cancels the pending acquire) does NOT cause an
            # over-release in the finally block below — the previous code
            # always released, corrupting the counter on timeout.
            acquired = False
            if self._global_semaphore:
                await asyncio.wait_for(
                    self._global_semaphore.acquire(),
                    timeout=30,
                )
                acquired = True

            try:
                from beever_atlas.api.sync import get_sync_runner

                runner = get_sync_runner()
                sync_type = effective.sync.sync_type or "auto"
                job_id = await runner.start_sync(channel_id, sync_type=sync_type)
                logger.info(
                    "SyncScheduler: triggered sync channel=%s job_id=%s",
                    channel_id,
                    job_id,
                )
            except ValueError as exc:
                logger.info(
                    "SyncScheduler: sync skipped channel=%s: %s",
                    channel_id,
                    exc,
                )
            finally:
                if acquired:
                    self.release_sync_semaphore()

        except asyncio.TimeoutError:
            logger.warning(
                "SyncScheduler: sync semaphore timeout channel=%s",
                channel_id,
            )
        except Exception as exc:
            logger.error(
                "SyncScheduler: sync failed channel=%s: %s",
                channel_id,
                exc,
                exc_info=True,
            )

    async def _execute_consolidation(self, channel_id: str) -> None:
        """Execute a scheduled consolidation for a channel."""
        try:
            from beever_atlas.services.pipeline_orchestrator import trigger_consolidation

            await trigger_consolidation(channel_id)
            logger.info("SyncScheduler: triggered consolidation channel=%s", channel_id)
        except Exception as exc:
            logger.error(
                "SyncScheduler: consolidation failed channel=%s: %s",
                channel_id,
                exc,
                exc_info=True,
            )
