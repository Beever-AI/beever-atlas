"""Sync trigger and status API endpoints."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.stores import get_stores

router = APIRouter(prefix="/api/channels", tags=["sync"])
logger = logging.getLogger(__name__)

_sync_runner = None


def get_sync_runner():
    global _sync_runner
    if _sync_runner is None:
        from beever_atlas.services.sync_runner import SyncRunner

        _sync_runner = SyncRunner()
    return _sync_runner


async def shutdown_sync_runner() -> None:
    """Gracefully stop in-flight sync tasks."""
    global _sync_runner
    if _sync_runner is None:
        return
    await _sync_runner.shutdown()
    _sync_runner = None


@router.post("/{channel_id}/sync")
async def trigger_sync(
    channel_id: str,
    sync_type: Literal["auto", "full", "incremental"] = Query(default="auto"),
    use_batch_api: bool = Query(default=False),
    connection_id: str | None = Query(default=None),
    principal: Principal = Depends(require_user),
) -> dict:
    """Trigger a sync job for the given channel."""
    await assert_channel_access(principal, channel_id)
    logger.info("Sync API: trigger requested for channel=%s sync_type=%s", channel_id, sync_type)

    # Resolve effective policy for cooldown and default sync_type
    stores = get_stores()
    try:
        from beever_atlas.services.policy_resolver import resolve_effective_policy

        effective = await resolve_effective_policy(channel_id)

        # Cooldown enforcement — bypassed when the last sync failed so users
        # can retry immediately without waiting out a penalty on a broken run.
        cooldown = effective.sync.min_sync_interval_minutes or 0
        if cooldown > 0:
            last_job = await stores.mongodb.get_sync_status(channel_id)
            if last_job and last_job.completed_at and last_job.status != "failed":
                completed = last_job.completed_at
                if completed.tzinfo is None:
                    completed = completed.replace(tzinfo=UTC)
                elapsed = datetime.now(tz=UTC) - completed
                if elapsed < timedelta(minutes=cooldown):
                    remaining = timedelta(minutes=cooldown) - elapsed
                    raise HTTPException(
                        status_code=429,
                        detail=(
                            f"Cooldown active. Try again in {int(remaining.total_seconds())}s."
                        ),
                        headers={
                            "Retry-After": str(int(remaining.total_seconds())),
                            "X-Cooldown-Remaining-Seconds": str(int(remaining.total_seconds())),
                        },
                    )

        # Use policy sync_type as default when caller sends "auto"
        if sync_type == "auto" and effective.sync.sync_type:
            resolved_sync_type = effective.sync.sync_type
        else:
            resolved_sync_type = sync_type
    except ImportError:
        resolved_sync_type = sync_type

    # Acquire global concurrency semaphore if scheduler is running
    scheduler = None
    try:
        from beever_atlas.services.scheduler import get_scheduler

        scheduler = get_scheduler()
        if scheduler:
            await scheduler.acquire_sync_semaphore()
    except ImportError:
        pass

    sync_runner = get_sync_runner()
    try:
        job_id = await sync_runner.start_sync(
            channel_id,
            sync_type=resolved_sync_type,
            use_batch_api=use_batch_api,
            connection_id=connection_id,
            owner_principal_id=principal.id,
        )
    except ValueError as exc:
        logger.info(
            "Sync API: trigger rejected for channel=%s: %s",
            channel_id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        if scheduler:
            scheduler.release_sync_semaphore()
    logger.info(
        "Sync API: trigger accepted for channel=%s job_id=%s",
        channel_id,
        job_id,
    )
    return {"job_id": job_id, "status": "started"}


@router.get("/{channel_id}/sync/history")
async def get_sync_history(
    channel_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    principal: Principal = Depends(require_user),
) -> list[dict]:
    """Return past sync jobs for a channel with pipeline progress details."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    jobs = await stores.mongodb.get_sync_jobs_for_channel(
        channel_id=channel_id,
        limit=limit,
    )
    return [
        {
            "job_id": job.id,
            "status": job.status,
            "sync_type": job.sync_type,
            "total_messages": job.total_messages,
            "parent_messages": getattr(job, "parent_messages", 0) or job.total_messages,
            "processed_messages": job.processed_messages,
            "total_batches": getattr(job, "total_batches", 0),
            "current_stage": getattr(job, "current_stage", None),
            "stage_timings": getattr(job, "stage_timings", {}),
            "stage_details": getattr(job, "stage_details", {}),
            "batch_results": getattr(job, "batch_results", []),
            "errors": job.errors,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        }
        for job in jobs
    ]


_STATUS_MAP = {
    "running": "syncing",
    "completed": "idle",
    "failed": "error",
}


@router.get("/{channel_id}/sync/status")
async def get_sync_status(
    channel_id: str,
    principal: Principal = Depends(require_user),
) -> dict:
    """Get the current sync progress for the given channel."""
    await assert_channel_access(principal, channel_id)
    stores = get_stores()
    job = await stores.mongodb.get_sync_status(channel_id)
    if job is None:
        logger.debug("Sync API: status channel=%s state=idle (no job)", channel_id)
        return {"state": "idle"}
    if job.status == "running":
        sync_runner = get_sync_runner()
        if not sync_runner.has_active_sync(channel_id):
            # Job was running but has no active task — process restarted or crashed before completion.
            _interrupted_errors = [
                "Sync was interrupted — server restarted or crashed before the job finished"
            ]
            logger.info(
                "Sync API: recovering stale running job channel=%s job_id=%s — marking failed",
                channel_id,
                job.id,
            )
            await stores.mongodb.complete_sync_job(
                job_id=job.id,
                status="failed",
                errors=_interrupted_errors,
                failed_stage="interrupted",
            )
            return {"state": "error", "job_id": job.id, "errors": _interrupted_errors}
    response = {
        "state": _STATUS_MAP.get(job.status, job.status),
        "job_id": job.id,
        "total_messages": job.total_messages,
        "parent_messages": getattr(job, "parent_messages", 0) or job.total_messages,
        "processed_messages": job.processed_messages,
        "current_batch": job.current_batch,
        "total_batches": getattr(job, "total_batches", 0),
        "batches_completed": getattr(job, "batches_completed", 0),
        "current_stage": getattr(job, "current_stage", None),
        "stage_timings": getattr(job, "stage_timings", {}),
        "stage_details": getattr(job, "stage_details", {}),
        "batch_results": getattr(job, "batch_results", []),
        "errors": job.errors,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "batch_job_state": getattr(job, "batch_job_state", None),
        "batch_job_elapsed_seconds": getattr(job, "batch_job_elapsed_seconds", None),
    }
    logger.debug(
        "Sync API: status channel=%s job_id=%s state=%s processed=%d/%d batch=%d",
        channel_id,
        job.id,
        response["state"],
        job.processed_messages,
        job.total_messages,
        job.current_batch,
    )
    return response
