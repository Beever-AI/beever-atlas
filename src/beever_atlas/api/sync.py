"""Sync trigger and status API endpoints."""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

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
) -> dict:
    """Trigger a sync job for the given channel."""
    logger.info("Sync API: trigger requested for channel=%s sync_type=%s", channel_id, sync_type)
    sync_runner = get_sync_runner()
    try:
        job_id = await sync_runner.start_sync(channel_id, sync_type=sync_type)
    except ValueError as exc:
        logger.info(
            "Sync API: trigger rejected for channel=%s: %s",
            channel_id,
            exc,
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    logger.info(
        "Sync API: trigger accepted for channel=%s job_id=%s",
        channel_id,
        job_id,
    )
    return {"job_id": job_id, "status": "started"}


_STATUS_MAP = {
    "running": "syncing",
    "completed": "idle",
    "failed": "error",
}


@router.get("/{channel_id}/sync/status")
async def get_sync_status(channel_id: str) -> dict:
    """Get the current sync progress for the given channel."""
    stores = get_stores()
    job = await stores.mongodb.get_sync_status(channel_id)
    if job is None:
        logger.debug("Sync API: status channel=%s state=idle (no job)", channel_id)
        return {"state": "idle"}
    if job.status == "running":
        sync_runner = get_sync_runner()
        if not sync_runner.has_active_sync(channel_id):
            logger.warning(
                "Sync API: recovering stale running status channel=%s job_id=%s",
                channel_id,
                job.id,
            )
            await stores.mongodb.complete_sync_job(
                job_id=job.id,
                status="failed",
                errors=[
                    "Recovered stale running job after process restart; "
                    "please retry sync."
                ],
            )
            job = await stores.mongodb.get_sync_status(channel_id)
            if job is None:
                return {"state": "idle"}
    response = {
        "state": _STATUS_MAP.get(job.status, job.status),
        "job_id": job.id,
        "total_messages": job.total_messages,
        "processed_messages": job.processed_messages,
        "current_batch": job.current_batch,
        "current_stage": getattr(job, "current_stage", None),
        "stage_timings": getattr(job, "stage_timings", {}),
        "stage_details": getattr(job, "stage_details", {}),
        "errors": job.errors,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
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
