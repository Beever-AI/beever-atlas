"""Sync trigger capability.

Framework-neutral implementation for openspec change ``atlas-mcp-server``
Phase 1 (task 1.5). Contains the cooldown-enforcement and job-creation
logic extracted from ``api/sync.py:38-127``.

The REST endpoint at ``api/sync.py`` continues to catch
:class:`~capabilities.errors.CooldownActive` and re-raise as
``HTTPException(429)`` with ``Retry-After`` header so the API response
shape is byte-identical.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from beever_atlas.capabilities.errors import (
    ChannelAccessDenied,
    CooldownActive,
    ServiceUnavailable,
)
from beever_atlas.infra.channel_access import assert_channel_access
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)


def get_sync_runner():
    """Return the SyncRunner singleton from api.sync (lazy import, patchable)."""
    from beever_atlas.api.sync import get_sync_runner as _get_runner
    return _get_runner()


async def resolve_effective_policy(channel_id: str):
    """Delegate to services.policy_resolver (lazy import, patchable)."""
    from beever_atlas.services.policy_resolver import resolve_effective_policy as _resolve
    return await _resolve(channel_id)


async def trigger_sync(
    principal_id: str,
    channel_id: str,
    sync_type: str = "incremental",
    use_batch_api: bool = False,
    connection_id: str | None = None,
) -> dict:
    """Trigger a sync job for *channel_id*.

    Enforces :func:`assert_channel_access` and the cooldown policy from
    ``services/policy_resolver``.  Raises:

    * :class:`~capabilities.errors.ChannelAccessDenied` — principal cannot
      access the channel.
    * :class:`~capabilities.errors.CooldownActive` — a completed sync ran
      within the cooldown window; ``exc.retry_after_seconds`` carries the
      remaining wait.

    On success returns::

        {"job_id": "...", "status_uri": "atlas://job/<id>", "status": "queued"}
    """
    try:
        await assert_channel_access(principal_id, channel_id)
    except Exception as exc:
        raise ChannelAccessDenied(channel_id) from exc

    try:
        stores_mod = get_stores()
    except Exception as exc:
        raise ServiceUnavailable("stores") from exc

    # Resolve effective policy for cooldown and default sync_type.
    resolved_sync_type = sync_type
    try:
        effective = await resolve_effective_policy(channel_id)

        cooldown = effective.sync.min_sync_interval_minutes or 0
        if cooldown > 0 and stores_mod is not None:
            last_job = await stores_mod.mongodb.get_sync_status(channel_id)
            if (
                last_job
                and last_job.completed_at
                and last_job.status != "failed"
            ):
                completed = last_job.completed_at
                if completed.tzinfo is None:
                    completed = completed.replace(tzinfo=UTC)
                elapsed = datetime.now(tz=UTC) - completed
                if elapsed < timedelta(minutes=cooldown):
                    remaining = timedelta(minutes=cooldown) - elapsed
                    raise CooldownActive(int(remaining.total_seconds()))

        # Use policy sync_type as default when caller sends "auto".
        if sync_type == "auto" and effective.sync.sync_type:
            resolved_sync_type = effective.sync.sync_type
    except (ChannelAccessDenied, CooldownActive):
        raise
    except ImportError:
        pass
    except Exception:
        logger.warning(
            "trigger_sync: policy resolution failed for channel=%s, proceeding without policy",
            channel_id,
        )

    # Acquire global concurrency semaphore if scheduler is running.
    scheduler = None
    try:
        from beever_atlas.services.scheduler import get_scheduler
        scheduler = get_scheduler()
        if scheduler:
            await scheduler.acquire_sync_semaphore()
    except (ImportError, Exception):
        scheduler = None

    sync_runner = get_sync_runner()
    try:
        job_id = await sync_runner.start_sync(
            channel_id,
            sync_type=resolved_sync_type,
            use_batch_api=use_batch_api,
            connection_id=connection_id,
            owner_principal_id=principal_id,
        )
    except ValueError as exc:
        logger.info(
            "trigger_sync: rejected for channel=%s: %s",
            channel_id,
            exc,
        )
        raise
    finally:
        if scheduler:
            try:
                scheduler.release_sync_semaphore()
            except Exception:
                pass

    logger.info(
        "trigger_sync: accepted for channel=%s job_id=%s principal=%s",
        channel_id,
        job_id,
        principal_id,
    )
    return {
        "job_id": job_id,
        "status_uri": f"atlas://job/{job_id}",
        "status": "queued",
    }


__all__ = ["trigger_sync"]
