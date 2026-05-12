"""Sync trigger and status API endpoints."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from beever_atlas.infra.auth import Principal, require_user
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.stores import get_stores

router = APIRouter(prefix="/api/channels", tags=["sync"])
logger = logging.getLogger(__name__)

# Match the worker's retry budget so the retrying-vs-abandoned split on
# the status payload mirrors the actual worker contract. Keeping the
# constant local (instead of importing from extraction_worker) avoids a
# circular import between the api and services packages.
_DEFAULT_MAX_RETRIES = 5

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

    # Reject syncs during an active embedding-provider migration. New
    # facts ingested mid-migration would land with empty vectors (the
    # pipeline's defensive fallback), forcing a manual back-fill later.
    # Better to fail-fast and tell the operator to retry once the
    # migration completes.
    from beever_atlas.llm.embedding_runtime import is_migration_in_progress

    if await is_migration_in_progress():
        raise HTTPException(
            status_code=409,
            detail={
                "error": "embedding_migration_in_progress",
                "message": (
                    "Sync is paused while an embedding migration runs. "
                    "Newly ingested facts would lack vectors. Retry "
                    "once the migration completes — see "
                    "/api/settings/embedding/migrate/status."
                ),
            },
        )

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
    """Get the current sync progress for the given channel.

    Phase 3 / Tasks 4.2.1-4.2.5 — the response carries the legacy
    payload unchanged AND four optional extensions: ``phases``,
    ``recent_events``, ``smoothed_eta_seconds``, ``retrying`` and
    ``abandoned``. Old clients that ignore the new fields keep working;
    the new UI uses them to render a phased progress card with a
    smoothed ETA and a retrying-vs-abandoned distinction.
    """
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

    # ------------------------------------------------------------------
    # Extended payload composition (Phase 3 / D6).
    # Each compose_* call swallows its own exceptions so a transient
    # store glitch never breaks the legacy response shape — the new
    # fields land as None / empty list and the UI falls back to the
    # legacy rendering.
    # ------------------------------------------------------------------
    counts = await _safe_counts(stores, channel_id)
    failure_split = await _safe_failure_split(stores, channel_id)
    overview_state = await _safe_overview_state(stores, channel_id)
    maintenance_progress = _safe_wiki_maintenance_progress()
    phases = _compose_phases(job, counts, overview_state, maintenance_progress)
    recent_events = _compose_recent_events(channel_id)
    smoothed_eta = _compute_smoothed_eta(counts)
    parse_failure_state = _compose_parse_failure_state(channel_id)

    # Merge rich activity_log entries from worker:* sync_jobs into the
    # user-facing job's stage_details so the SyncProgressV2 UI sees them.
    # The decoupled ExtractionWorker writes per-batch stage_output rows
    # to a synthetic job_id; without this merge the UI's Pipeline
    # Activity tab stays empty during the extraction phase.
    merged_stage_details: dict[str, Any] = dict(getattr(job, "stage_details", {}) or {})
    try:
        if job.started_at is not None:
            since_iso = job.started_at.isoformat()
        else:
            since_iso = None
        merged_log = await stores.mongodb.list_recent_activity_log(
            channel_id=channel_id,
            since_iso=since_iso,
            limit=200,
        )
        if merged_log:
            existing = list(merged_stage_details.get("activity_log") or [])
            # Dedup by (agent, batch_idx, message, type) — worker tee can
            # in principle land the same entry on multiple jobs.
            seen: set[tuple] = set()
            combined: list[dict[str, Any]] = []
            for entry in existing + merged_log:
                key = (
                    entry.get("agent"),
                    entry.get("batch_idx"),
                    entry.get("type"),
                    (entry.get("message") or "")[:60],
                )
                if key in seen:
                    continue
                seen.add(key)
                combined.append(entry)
            merged_stage_details["activity_log"] = combined
    except Exception:  # noqa: BLE001 — never break /sync/status on a merge glitch
        logger.exception(
            "Sync API: activity_log merge failed channel=%s job=%s",
            channel_id,
            job.id,
        )

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
        "stage_details": merged_stage_details,
        "batch_results": getattr(job, "batch_results", []),
        "errors": job.errors,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "batch_job_state": getattr(job, "batch_job_state", None),
        "batch_job_elapsed_seconds": getattr(job, "batch_job_elapsed_seconds", None),
        # New optional fields — every legacy field above is preserved
        # untouched. None / empty defaults keep old clients unaffected.
        "phases": phases,
        "recent_events": recent_events,
        "smoothed_eta_seconds": smoothed_eta,
        "retrying": failure_split["retrying"],
        "abandoned": failure_split["abandoned"],
        # ``unified-llm-wiki-graph-redesign`` — wiki layer signals.
        # The frontend SyncMonitor + WikiTab parse-failure banner
        # consume these fields. Old clients ignore unknown keys.
        "parse_failure_state": parse_failure_state,
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


# ---------------------------------------------------------------------------
# Extended-payload helpers (Phase 3 / D6)
# ---------------------------------------------------------------------------


async def _safe_counts(stores: Any, channel_id: str) -> dict[str, int]:
    """Best-effort ``count_channel_messages_by_status`` wrapper.

    Returns the zero-filled shape on failure so the phase composer can
    proceed without conditional logic in two places.
    """
    try:
        return await stores.mongodb.count_channel_messages_by_status(channel_id)
    except Exception:  # noqa: BLE001
        logger.debug(
            "Sync API: count_channel_messages_by_status raised channel=%s — defaulting to zeros",
            channel_id,
            exc_info=True,
        )
        return {"pending": 0, "extracting": 0, "done": 0, "failed": 0}


async def _safe_failure_split(stores: Any, channel_id: str) -> dict[str, int]:
    """Best-effort retrying/abandoned split."""
    try:
        return await stores.mongodb.count_channel_messages_failure_subtypes(
            channel_id,
            max_retries=_DEFAULT_MAX_RETRIES,
        )
    except Exception:  # noqa: BLE001
        logger.debug(
            "Sync API: count_channel_messages_failure_subtypes raised channel=%s — defaulting to zeros",
            channel_id,
            exc_info=True,
        )
        return {"retrying": 0, "abandoned": 0}


async def _safe_overview_state(stores: Any, channel_id: str) -> dict[str, Any]:
    """Resolve the overview phase's state.

    Returns ``{"state": "done"|"in_flight"|"skipped"|"pending"}``.
    Reads three signals in priority order:

      1. feature flag ``AUTO_OVERVIEW_WIKI`` off → ``skipped``
      2. an overview row exists for this channel → ``done``
      3. the auto-overview subscriber has the channel in its in-flight
         set → ``in_flight``
      4. otherwise → ``pending``
    """
    out: dict[str, Any] = {"state": "pending"}
    # 1) feature flag
    try:
        from beever_atlas.infra.config import get_settings

        if not bool(getattr(get_settings(), "auto_overview_wiki", True)):
            out["state"] = "skipped"
            return out
    except Exception:  # noqa: BLE001
        pass

    # 2) existing overview row?
    # The wiki is stored in the ``wiki_versions`` collection as the
    # ``structure.pages`` array on each version document — NOT in a
    # separate ``wiki_pages`` collection. The previous lookup against
    # ``wiki_pages`` always returned None (collection is unused), so
    # the phase was stuck at ``pending`` even after the overview was
    # successfully built — caught by scripts/test_pipeline_design.py
    # against a hand-triggered /api/wiki/refresh.
    try:
        db = getattr(stores.mongodb, "db", None)
        if db is not None:
            doc = await db["wiki_versions"].find_one(
                {
                    "channel_id": channel_id,
                    "structure.pages.id": "overview",
                },
                projection={"_id": 1},
            )
            if doc is not None:
                out["state"] = "done"
                return out
    except Exception:  # noqa: BLE001
        logger.debug(
            "Sync API: wiki_versions overview lookup raised channel=%s",
            channel_id,
            exc_info=True,
        )

    # 3) in-flight in the subscriber?
    try:
        from beever_atlas.services.auto_overview_subscriber import (
            get_auto_overview_subscriber,
        )

        subscriber = get_auto_overview_subscriber()
        if subscriber is not None and subscriber.is_inflight(channel_id):
            out["state"] = "in_flight"
            # Surface the attempt start-time so the frontend can render
            # elapsed seconds + a Retry button if the build hangs.
            started_at = subscriber.attempted_started_at(channel_id)
            if started_at is not None:
                out["started_at"] = started_at.isoformat()
            return out
    except Exception:  # noqa: BLE001
        pass

    return out


def _safe_wiki_maintenance_progress() -> dict[str, int] | None:
    """Best-effort read of the WikiMaintainer's rolling rewrite counters.

    Returns ``{"done": apply_60min, "dirty": mark_dirty_60min}`` when the
    maintainer singleton is registered, else ``None``. Counters are
    process-global (not channel-scoped) — the maintainer does not bucket
    its rolling lists by channel, so the fraction reported here is a
    rough proxy for "is the maintainer still working" rather than
    a precise per-channel completion ratio. The frontend gracefully
    handles ``None`` for these fields. Reuses the same source as the
    ``/api/admin/wiki-maintainer/metrics`` endpoint.
    """
    try:
        from beever_atlas.services.wiki_maintainer import get_wiki_maintainer

        maintainer = get_wiki_maintainer()
        if maintainer is None:
            return None
        snapshot = maintainer._in_memory_metrics_snapshot()
        return {
            "done": int(snapshot.get("apply_update_count_60min", 0) or 0),
            # ``mark_dirty_count_5min`` is the only mark-dirty rolling slice
            # exposed today; pair it with the 60-min apply count as a rough
            # "remaining + completed" total. Better approximation than
            # leaving the bar empty.
            "dirty": int(snapshot.get("mark_dirty_count_5min", 0) or 0),
        }
    except Exception:  # noqa: BLE001 — observability is best-effort
        logger.debug(
            "Sync API: wiki_maintenance progress lookup raised — defaulting to None",
            exc_info=True,
        )
        return None


def _compose_phases(
    job: Any,
    counts: dict[str, int],
    overview_state: dict[str, Any],
    maintenance_progress: dict[str, int] | None = None,
) -> list[dict[str, Any]]:
    """Build the four-entry ``phases`` array.

    Order is fixed: ``fetched`` → ``extracting`` → ``wiki_maintenance``
    → ``overview_wiki``. Each entry carries ``name`` + ``state`` plus
    optional ``done`` / ``total`` / ``duration_ms`` / ``last_event_label``
    keys per the spec. Missing fields are omitted (None values would
    pollute the response shape unnecessarily).
    """
    phases: list[dict[str, Any]] = []

    # Phase 1: fetched. Done iff the job has completed the fetch stage,
    # which is implied by the status no longer being "running" OR by
    # any extraction-status row existing for the channel (the sync
    # writer persists rows AFTER the fetch loop succeeds). Duration
    # comes from completed_at - started_at on the job row.
    fetched_state = "in_flight"
    if getattr(job, "status", None) in ("completed",):
        fetched_state = "done"
    elif sum(counts.values()) > 0:
        fetched_state = "done"
    elif getattr(job, "status", None) == "failed":
        fetched_state = "failed"
    duration_ms: int | None = None
    started = getattr(job, "started_at", None)
    completed = getattr(job, "completed_at", None)
    if started is not None and completed is not None:
        try:
            duration_ms = int((completed - started).total_seconds() * 1000)
        except Exception:  # noqa: BLE001
            duration_ms = None
    fetched_entry: dict[str, Any] = {"name": "fetched", "state": fetched_state}
    if duration_ms is not None:
        fetched_entry["duration_ms"] = duration_ms
    phases.append(fetched_entry)

    # Phase 2: extracting. Counts come from the channel_messages
    # aggregate. State is in_flight when there is any pending or
    # extracting work, done when total>0 and pending+extracting=0,
    # failed when every row is in failed (no done, no pending), else
    # pending (channel has no rows yet).
    pending = int(counts.get("pending", 0) or 0)
    extracting = int(counts.get("extracting", 0) or 0)
    done = int(counts.get("done", 0) or 0)
    failed = int(counts.get("failed", 0) or 0)
    total = pending + extracting + done + failed
    if total == 0:
        extract_state = "pending"
    elif pending + extracting > 0:
        extract_state = "in_flight"
    elif done == 0 and failed > 0:
        extract_state = "failed"
    else:
        extract_state = "done"
    phases.append(
        {
            "name": "extracting",
            "state": extract_state,
            "done": done,
            "total": total,
        }
    )

    # Phase 3: wiki_maintenance. Treated as in_flight while extraction
    # is still producing facts (the maintainer is debounced behind
    # extraction events) and as done once extraction has settled.
    # ``done`` / ``total`` come from the WikiMaintainer's rolling
    # apply/mark-dirty counters when the singleton is registered. The
    # counters are process-global (not channel-scoped), so the fraction
    # is a rough proxy for "the maintainer is making progress" rather
    # than a precise per-channel ratio. The frontend gracefully omits
    # the fraction when these fields are missing.
    if total == 0:
        maintenance_state = "pending"
    elif pending + extracting > 0:
        maintenance_state = "in_flight"
    else:
        maintenance_state = "done"
    maintenance_entry: dict[str, Any] = {
        "name": "wiki_maintenance",
        "state": maintenance_state,
    }
    if maintenance_progress is not None:
        done_count = maintenance_progress.get("done", 0)
        dirty_count = maintenance_progress.get("dirty", 0)
        maintenance_entry["done"] = done_count
        # ``total`` = pages still dirty + pages already rewritten this
        # rolling window — gives the UI a "rewrote X of Y" fraction.
        maintenance_entry["total"] = done_count + dirty_count
    phases.append(maintenance_entry)

    # Phase 4: overview_wiki. State decided by ``_safe_overview_state``,
    # then clamped by the memory-then-wiki gate: the overview cannot
    # legitimately be ``in_flight`` while extraction is still pending —
    # the AutoOverviewSubscriber gates on ``pending+extracting=0`` and
    # would bail out anyway. Without this clamp the subscriber's
    # transient "in-flight" set during a gate-check causes the UI to
    # flicker ``in_flight → pending`` (forward-only violation observed
    # in scripts/test_pipeline_design.py).
    overview_state_str = str(overview_state.get("state", "pending"))
    if extract_state in ("pending", "in_flight") and overview_state_str == "in_flight":
        overview_state_str = "pending"
    overview_entry: dict[str, Any] = {
        "name": "overview_wiki",
        "state": overview_state_str,
    }
    # Surface the subscriber's attempt-start ISO timestamp when the
    # phase is genuinely in-flight so the WikiTab can render a live
    # elapsed-time stamp and a Retry button if the build hangs.
    if overview_state_str == "in_flight":
        started_at = overview_state.get("started_at")
        if isinstance(started_at, str) and started_at:
            overview_entry["started_at"] = started_at
    phases.append(overview_entry)

    return phases


def _compose_recent_events(channel_id: str) -> list[dict[str, Any]]:
    """Read the most-recent pipeline events for ``channel_id``.

    Returns an empty list on any failure — the activity feed is
    decorative; never let a buffer hiccup take down the status
    endpoint.

    The redesign extends the per-event payload with ``event_type``
    (``message_processing``, ``agent_state``, ``wiki_update``,
    ``cost_summary``, ``parse_failure``, or ``legacy``) and an optional
    ``payload`` dict. The SyncMonitor frontend consumes the structured
    fields; legacy clients can keep reading ``stage`` + ``label``.
    Limit raised to 30 so the live monitor's panes have enough
    backbuffer.
    """
    try:
        from beever_atlas.services.pipeline_events import get_pipeline_events

        events = get_pipeline_events().recent_for(channel_id, limit=30)
        return [
            {
                "ts": evt.ts.isoformat(),
                "stage": evt.stage,
                "label": evt.label,
                "event_type": getattr(evt, "event_type", "legacy"),
                "payload": getattr(evt, "payload", None),
            }
            for evt in events
        ]
    except Exception:  # noqa: BLE001
        logger.debug(
            "Sync API: recent_events composition raised channel=%s",
            channel_id,
            exc_info=True,
        )
        return []


def _compose_parse_failure_state(channel_id: str) -> dict[str, Any]:
    """Compose the parse-failure banner state for the WikiTab.

    Returns ``{count_last_10_min, threshold, should_show_banner}``.
    The banner threshold is 3 (per design D7); the frontend renders the
    Retry / Dismiss / Details actions when ``should_show_banner=True``.
    Decorative: any error returns the safe-empty payload.
    """
    try:
        from beever_atlas.services.pipeline_events import get_pipeline_events

        count = get_pipeline_events().parse_failure_count_last_10_min(channel_id)
        return {
            "count_last_10_min": count,
            "threshold": 3,
            "should_show_banner": count >= 3,
        }
    except Exception:  # noqa: BLE001
        return {
            "count_last_10_min": 0,
            "threshold": 3,
            "should_show_banner": False,
        }


def _compute_smoothed_eta(counts: dict[str, int]) -> int | None:
    """Derive the smoothed ETA from the worker's tick samples.

    Returns ``None`` when:
      * no worker is registered (early lifespan / inline mode),
      * fewer than 3 successful-claim samples are in the EWMA window,
      * the rate is zero (no successful claims in the window),
      * the calculator raises for any other reason.
    """
    try:
        from beever_atlas.services.eta_calculator import smoothed_eta
        from beever_atlas.services.extraction_worker import get_extraction_worker

        worker = get_extraction_worker()
        if worker is None:
            return None
        samples = worker.tick_samples_for_eta()
        remaining = int(counts.get("pending", 0) or 0) + int(counts.get("extracting", 0) or 0)
        return smoothed_eta(samples, remaining=remaining, now=time.monotonic())
    except Exception:  # noqa: BLE001
        logger.debug(
            "Sync API: smoothed_eta computation raised — defaulting to None",
            exc_info=True,
        )
        return None
