"""Sync runner — orchestrates channel sync jobs end-to-end.

Manages per-channel sync lifecycle:
  - Guards against concurrent syncs on the same channel.
  - Determines incremental vs. full sync automatically.
  - Fetches all messages via cursor-based pagination.
  - Delegates batch processing to BatchProcessor.
  - Records sync job status and activity in MongoDB.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

from beever_atlas.adapters import get_adapter
from beever_atlas.infra.config import get_settings
from beever_atlas.services.batch_processor import BatchProcessor
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)


def _coerce_since_timestamp(value: Any | None) -> datetime | None:
    """Normalize persisted sync cursors to timezone-aware datetimes."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed
    raise TypeError(f"Unsupported sync cursor type: {type(value)!r}")


class SyncRunner:
    """Orchestrates channel sync jobs using BatchProcessor and ADK pipeline."""

    def __init__(self) -> None:
        self._batch_processor = BatchProcessor()
        self._active_tasks: dict[str, asyncio.Task[None]] = {}

    def _is_task_active(self, channel_id: str) -> bool:
        """Return True when this process has an unfinished sync task."""
        task = self._active_tasks.get(channel_id)
        if task is None:
            return False
        if task.done():
            self._active_tasks.pop(channel_id, None)
            return False
        return True

    def has_active_sync(self, channel_id: str) -> bool:
        """Public check used by API status endpoints."""
        return self._is_task_active(channel_id)

    async def start_sync(
        self,
        channel_id: str,
        sync_type: str = "auto",
    ) -> str:
        """Kick off a sync for *channel_id* and return the new job_id.

        Args:
            channel_id: Platform channel identifier.
            sync_type: ``"auto"`` (default), ``"full"``, or ``"incremental"``.

        Returns:
            The MongoDB SyncJob ID for the created job.

        Raises:
            ValueError: If a sync is already running for this channel.
        """
        stores = get_stores()
        settings = get_settings()
        if sync_type not in {"auto", "full", "incremental"}:
            raise ValueError(f"Invalid sync_type '{sync_type}'. Use one of: auto, full, incremental.")

        # 1. Guard: no concurrent sync for the same channel.
        existing = await stores.mongodb.get_sync_status(channel_id)
        if existing is not None and existing.status == "running":
            if self._is_task_active(channel_id):
                raise ValueError(
                    f"Sync already running for channel {channel_id} "
                    f"(job_id={existing.id})."
                )
            # Process restarted (or prior task crashed) and left a stale running
            # row behind; close it so the channel can be synced again.
            logger.warning(
                "SyncRunner: recovering stale running job channel=%s job_id=%s "
                "started_at=%s processed=%d/%d batch=%d",
                channel_id,
                existing.id,
                existing.started_at,
                existing.processed_messages,
                existing.total_messages,
                existing.current_batch,
            )
            await stores.mongodb.complete_sync_job(
                job_id=existing.id,
                status="failed",
                errors=[
                    "Recovered stale running job after process restart; "
                    "safe to retry sync."
                ],
            )

        # 2. Determine sync mode.
        sync_state = await stores.mongodb.get_channel_sync_state(channel_id)

        if sync_type == "auto":
            resolved_type = "incremental" if sync_state is not None else "full"
        else:
            resolved_type = sync_type

        since = None
        if resolved_type == "incremental" and sync_state is not None:
            since = sync_state.last_sync_ts

        # 3. Fetch all messages via cursor-based pagination.
        logger.info(
            "SyncRunner: fetch start channel=%s resolved_type=%s since=%s",
            channel_id,
            resolved_type,
            since,
        )
        messages = await self._fetch_all_messages(channel_id, since=since)

        # If incremental sync found nothing, auto-fallback to full sync
        if not messages and resolved_type == "incremental":
            logger.info(
                "SyncRunner: incremental sync found no new messages for channel %s, falling back to full sync.",
                channel_id,
            )
            resolved_type = "full"
            since = None
            messages = await self._fetch_all_messages(channel_id, since=None)

        if not messages:
            logger.info(
                "SyncRunner: no messages for channel %s (%s sync).",
                channel_id,
                resolved_type,
            )

        # 4. Fetch thread replies for messages with reply_count > 0.
        messages = await self._fetch_thread_replies(channel_id, messages)

        # 5. Get channel info for the human-readable name.
        adapter = get_adapter()
        channel_info = await adapter.get_channel_info(channel_id)
        channel_name = channel_info.name

        # 6. Create sync job in MongoDB.
        job = await stores.mongodb.create_sync_job(
            channel_id=channel_id,
            sync_type=resolved_type,
            total_messages=len(messages),
            batch_size=settings.sync_batch_size,
        )
        job_id: str = job.id

        # 7. Launch background task and track it.
        task = asyncio.create_task(
            self._run_sync(
                job_id=job_id,
                channel_id=channel_id,
                channel_name=channel_name,
                messages=messages,
            )
        )
        self._active_tasks[channel_id] = task

        logger.info(
            "SyncRunner: started %s sync for channel %s — job_id=%s, "
            "%d messages to process.",
            resolved_type,
            channel_id,
            job_id,
            len(messages),
        )

        return job_id

    async def _fetch_all_messages(
        self,
        channel_id: str,
        since: datetime | str | None = None,
    ) -> list[Any]:
        """Fetch all messages via cursor-based pagination.

        The bridge adapter caps each page at 500 messages. We continue until
        we hit ``settings.sync_max_messages`` or the adapter returns nothing.

        Args:
            channel_id: Channel to fetch from.
            since: Timestamp cursor for incremental fetches (``None`` for full).

        Returns:
            Flat list of NormalizedMessage objects.
        """
        settings = get_settings()
        adapter = get_adapter()
        all_messages: list[Any] = []
        cursor = _coerce_since_timestamp(since)

        while len(all_messages) < settings.sync_max_messages:
            page_num = (len(all_messages) // 500) + 1
            batch = await adapter.fetch_history(channel_id, since=cursor, limit=500)
            if not batch:
                logger.info(
                    "SyncRunner: fetch page=%d channel=%s empty; stopping.",
                    page_num,
                    channel_id,
                )
                break

            # Some adapters treat `since` as inclusive, so filter strictly newer
            # messages to avoid duplicates and cursor stalls.
            if cursor is not None:
                batch = [
                    m
                    for m in batch
                    if getattr(m, "timestamp", None) and m.timestamp > cursor
                ]
            if not batch:
                logger.info(
                    "SyncRunner: fetch page=%d channel=%s had no newer rows; stopping.",
                    page_num,
                    channel_id,
                )
                break

            remaining = settings.sync_max_messages - len(all_messages)
            if len(batch) > remaining:
                batch = batch[:remaining]
            all_messages.extend(batch)

            latest_ts = batch[-1].timestamp
            if cursor is not None and latest_ts <= cursor:
                logger.warning(
                    "SyncRunner: cursor did not advance for channel %s; stopping pagination.",
                    channel_id,
                )
                break
            logger.info(
                "SyncRunner: fetch page=%d channel=%s got=%d total=%d latest_ts=%s",
                page_num,
                channel_id,
                len(batch),
                len(all_messages),
                latest_ts,
            )
            cursor = latest_ts

        return all_messages

    async def _fetch_thread_replies(
        self,
        channel_id: str,
        messages: list[Any],
    ) -> list[Any]:
        """Fetch thread replies for messages with reply_count > 0.

        Inserts replies adjacent to their parent so the batch processor
        groups them in the same batch for thread context resolution.

        Uses a semaphore to respect Slack API rate limits (Tier 3).
        """
        adapter = get_adapter()
        sem = asyncio.Semaphore(3)

        # Identify thread parents
        thread_parents = [
            m for m in messages
            if getattr(m, "reply_count", 0) > 0
        ]

        if not thread_parents:
            return messages

        logger.info(
            "SyncRunner: fetching thread replies for %d threads in channel %s",
            len(thread_parents),
            channel_id,
        )

        # Fetch replies concurrently with semaphore
        async def _fetch_one(msg: Any) -> tuple[str, list[Any]]:
            thread_id = getattr(msg, "message_id", "") or getattr(msg, "ts", "")
            async with sem:
                try:
                    replies = await adapter.fetch_thread(channel_id, thread_id)
                    # Exclude the parent message (Slack includes it as first reply)
                    replies = [
                        r for r in replies
                        if getattr(r, "message_id", "") != thread_id
                    ]
                    return (thread_id, replies)
                except Exception as e:
                    logger.warning(
                        "SyncRunner: failed to fetch thread %s: %s",
                        thread_id,
                        e,
                    )
                    return (thread_id, [])

        results = await asyncio.gather(*[_fetch_one(m) for m in thread_parents])
        thread_replies: dict[str, list[Any]] = dict(results)

        # Insert replies after their parent in the message list
        merged: list[Any] = []
        total_replies = 0
        for m in messages:
            merged.append(m)
            mid = getattr(m, "message_id", "") or getattr(m, "ts", "")
            if mid in thread_replies:
                replies = thread_replies[mid]
                merged.extend(replies)
                total_replies += len(replies)

        logger.info(
            "SyncRunner: fetched %d thread replies across %d threads for channel %s",
            total_replies,
            len(thread_parents),
            channel_id,
        )

        # Deduplicate by message_id (conversations.replies may include the parent)
        seen_ids: set[str] = set()
        deduped: list[Any] = []
        for m in merged:
            mid = getattr(m, "message_id", "") or getattr(m, "ts", "")
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            deduped.append(m)

        removed = len(merged) - len(deduped)
        if removed:
            logger.info("SyncRunner: removed %d duplicate messages after thread merge", removed)

        return deduped

    async def _run_sync(
        self,
        job_id: str,
        channel_id: str,
        channel_name: str,
        messages: list[Any],
    ) -> None:
        """Execute the full sync, update job status, and clean up the task entry.

        Called as an asyncio Task — errors are caught and recorded rather than
        propagated, so the caller's event loop is never disrupted.
        """
        stores = get_stores()
        logger.info(
            "SyncRunner: run start job_id=%s channel=%s messages=%d",
            job_id,
            channel_id,
            len(messages),
        )

        try:
            result = await self._batch_processor.process_messages(
                messages=messages,
                channel_id=channel_id,
                channel_name=channel_name,
                sync_job_id=job_id,
            )

            # Determine last_sync_ts from the latest TOP-LEVEL message only.
            # Thread replies may have older timestamps that would cause cursor drift.
            last_ts: str | None = None
            if messages:
                top_level = [
                    m for m in messages
                    if not getattr(m, "thread_id", None)
                    or getattr(m, "thread_id", None) == getattr(m, "message_id", "")
                ]
                if top_level:
                    timestamps = [
                        getattr(m, "timestamp", None) for m in top_level
                        if getattr(m, "timestamp", None) is not None
                    ]
                    if timestamps:
                        max_ts = max(timestamps)
                        last_ts = max_ts.isoformat() if hasattr(max_ts, "isoformat") else str(max_ts)

            # Mark job complete.
            sync_status = "failed" if result.errors else "completed"
            sync_errors = None
            if result.errors:
                sync_errors = [
                    f"batch={err.get('batch_num')} error={err.get('error')}"
                    for err in result.errors
                ]
            await stores.mongodb.complete_sync_job(
                job_id=job_id,
                status=sync_status,
                errors=sync_errors,
            )

            # Update channel sync state.
            if last_ts is not None:
                await stores.mongodb.update_channel_sync_state(
                    channel_id=channel_id,
                    last_sync_ts=last_ts,
                    increment=len(messages),
                )

            # Build per-batch breakdowns for sync history.
            from dataclasses import asdict

            batch_summaries = [asdict(b) for b in result.batch_breakdowns]

            # Log activity with results_summary.
            await stores.mongodb.log_activity(
                event_type="sync_failed" if result.errors else "sync_completed",
                channel_id=channel_id,
                details={
                    "job_id": job_id,
                    "channel_name": channel_name,
                    "total_facts": result.total_facts,
                    "total_entities": result.total_entities,
                    "total_relationships": result.total_relationships,
                    "total_messages": len(messages),
                    "error_count": len(result.errors),
                    "results_summary": batch_summaries,
                },
            )

            logger.info(
                "SyncRunner: run complete job_id=%s channel=%s status=%s facts=%d entities=%d errors=%d",
                job_id,
                channel_id,
                sync_status,
                result.total_facts,
                result.total_entities,
                len(result.errors),
            )

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "SyncRunner: job %s failed: %s",
                job_id,
                exc,
                exc_info=True,
            )

            await stores.mongodb.complete_sync_job(
                job_id=job_id,
                status="failed",
                errors=[str(exc)],
            )

            await stores.mongodb.log_activity(
                event_type="sync_failed",
                channel_id=channel_id,
                details={"job_id": job_id, "error": str(exc)},
            )

        finally:
            self._active_tasks.pop(channel_id, None)

    async def shutdown(self) -> None:
        """Cancel all active sync tasks gracefully."""
        if not self._active_tasks:
            return

        logger.info(
            "SyncRunner: shutting down — cancelling %d active task(s).",
            len(self._active_tasks),
        )

        tasks = list(self._active_tasks.values())
        for task in tasks:
            task.cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, res in zip(tasks, results):
            if isinstance(res, Exception) and not isinstance(res, asyncio.CancelledError):
                logger.warning("SyncRunner: task raised during shutdown: %s", res)

        self._active_tasks.clear()
        logger.info("SyncRunner: shutdown complete.")
