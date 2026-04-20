"""Sync scheduler — manages scheduled ingestion and consolidation jobs."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from apscheduler import AsyncScheduler
from apscheduler.datastores.mongodb import MongoDBDataStore
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
        self._mongodb_uri = mongodb_uri
        self._data_store = MongoDBDataStore(mongodb_uri, database="beever_atlas")
        self._scheduler = AsyncScheduler(data_store=self._data_store)
        self._global_semaphore: asyncio.Semaphore | None = None
        self._started = False

    async def startup(self) -> None:
        """Load all channel policies, register jobs, start the scheduler."""
        from beever_atlas.stores import get_stores

        stores = get_stores()
        defaults = await stores.mongodb.get_global_defaults()
        self._global_semaphore = asyncio.Semaphore(defaults.max_concurrent_syncs)

        # Load all channel policies and register jobs
        policies = await stores.mongodb.list_channel_policies()
        for policy in policies:
            if not policy.enabled:
                continue
            await self._register_sync_job(policy.channel_id, policy)
            await self._register_consolidation_job(policy.channel_id, policy)

        await self._scheduler.__aenter__()
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
                max_running_jobs=1,
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
                    max_running_jobs=1,
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
                    max_running_jobs=1,
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
