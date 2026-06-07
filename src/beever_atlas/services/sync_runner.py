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
from datetime import UTC, datetime, timedelta
from typing import Any, Awaitable, Callable

from beever_atlas.adapters import get_adapter
from beever_atlas.adapters.bridge import ChatBridgeAdapter
from beever_atlas.infra.config import get_settings
from beever_atlas.models.persistence import ChannelMessage
from beever_atlas.services.batch_processor import BatchProcessor
from beever_atlas.stores import get_stores

logger = logging.getLogger(__name__)


def _normalized_to_channel_messages(messages: list[Any]) -> list[ChannelMessage]:
    """Convert ``NormalizedMessage``-shaped objects to ``ChannelMessage`` rows
    for upsert into the durable Message Store.

    ``source_id`` is derived from the message's ``platform`` field — for chat
    adapters that's "slack" | "discord" | "teams"; file imports use "file";
    push sources set their own registered ``source_id`` directly on the
    payload before reaching this helper.

    Tolerates duck-typed dicts (used by the file importer) in addition to
    dataclass-shaped messages so callers do not have to unify their types.
    """
    rows: list[ChannelMessage] = []
    now = datetime.now(tz=UTC)

    def _read(obj: Any, key: str, default: Any = None) -> Any:
        if isinstance(obj, dict):
            return obj.get(key, default)
        return getattr(obj, key, default)

    for m in messages:
        platform = str(_read(m, "platform") or "unknown")
        source_id = str(_read(m, "source_id") or platform)
        message_id = str(_read(m, "message_id") or "")
        if not message_id:
            # No identity to dedup on — skip (defensive; should never happen
            # for well-formed adapter output).
            continue
        timestamp = _read(m, "timestamp")
        if timestamp is None:
            timestamp = now
        try:
            rows.append(
                ChannelMessage(
                    source_id=source_id,
                    channel_id=str(_read(m, "channel_id") or ""),
                    # Discord-only guild id; empty for other platforms. Mirrors
                    # how ``platform``/``source_id`` are read above so the
                    # permalink resolver downstream can build Discord URLs.
                    guild_id=str(_read(m, "guild_id") or ""),
                    message_id=message_id,
                    channel_name=str(_read(m, "channel_name") or ""),
                    timestamp=timestamp,
                    author=str(_read(m, "author") or ""),
                    author_name=str(_read(m, "author_name") or ""),
                    author_image=str(_read(m, "author_image") or ""),
                    content=str(_read(m, "content") or ""),
                    thread_id=_read(m, "thread_id"),
                    attachments=list(_read(m, "attachments") or []),
                    reactions=list(_read(m, "reactions") or []),
                    reply_count=int(_read(m, "reply_count") or 0),
                    raw_metadata=dict(_read(m, "raw_metadata") or {}),
                )
            )
        except Exception as exc:  # noqa: BLE001 — best-effort conversion
            # Preserve channel_id + source_id + exc class in the log so an
            # operator can grep the WARN line and pinpoint a stuck channel.
            logger.warning(
                "_normalized_to_channel_messages: skipped source_id=%s "
                "channel_id=%s message_id=%s exc=%s: %.200s",
                source_id,
                str(_read(m, "channel_id") or ""),
                message_id,
                type(exc).__name__,
                str(exc),
            )
    return rows


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
        # Consolidation is handled by pipeline_orchestrator (not SyncRunner)

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
        use_batch_api: bool = False,
        connection_id: str | None = None,
        owner_principal_id: str | None = None,
        progress_callback: Callable[[float, str], Awaitable[None]] | None = None,
    ) -> str:
        """Kick off a sync for *channel_id* and return the new job_id.

        Args:
            channel_id: Platform channel identifier.
            sync_type: ``"auto"`` (default), ``"full"``, or ``"incremental"``.
            owner_principal_id: Principal id stamped on the created
                ``sync_jobs`` row; required for MCP ownership checks in
                ``capabilities.jobs.get_job_status``.
            progress_callback: Optional async callable ``(fraction: float,
                message: str) -> None`` invoked at key milestones during the
                sync. Intended for MCP ``ctx.report_progress`` wiring (Phase
                6+). The callback slot is added here so callers can attach it
                without a further API change; the ``_run_sync`` inner loop does
                not yet forward calls to this callback — see Phase 5.5 gap
                note in the commit message.

        Returns:
            The MongoDB SyncJob ID for the created job.

        Raises:
            ValueError: If a sync is already running for this channel.
        """
        # Store callback for future use by _run_sync (Phase 6+ wiring).
        self._progress_callback = progress_callback
        stores = get_stores()
        settings = get_settings()
        # delete-channel-v2 Wave 0 — purge guard at FUNCTION ENTRY. Placed
        # before the stale-job recovery write below so a purging channel is
        # never touched (the recovery path would otherwise write sync_jobs
        # rows for a channel being torn down). Raising ValueError matches the
        # existing "already running" contract — callers (scheduler, API)
        # already catch ValueError and skip.
        if await stores.mongodb.is_purging(channel_id):
            logger.info(
                "SyncRunner: refusing start_sync — channel is purging channel=%s",
                channel_id,
            )
            raise ValueError(f"Channel {channel_id} is being purged; sync refused.")
        if sync_type not in {"auto", "full", "incremental"}:
            raise ValueError(
                f"Invalid sync_type '{sync_type}'. Use one of: auto, full, incremental."
            )

        # Stale job recovery: mark jobs stuck "running" for > threshold as "failed"
        stale_threshold = datetime.now(tz=UTC) - timedelta(hours=settings.stale_job_threshold_hours)
        stale_jobs = (
            await stores.mongodb.db["sync_jobs"]
            .find(
                {
                    "channel_id": channel_id,
                    "status": "running",
                    "started_at": {"$lt": stale_threshold},
                }
            )
            .to_list(length=10)
        )
        for stale in stale_jobs:
            stale_id = stale.get("id", "?")
            if not self._is_task_active(channel_id):
                logger.warning(
                    "SyncRunner: recovering stale job channel=%s job_id=%s started_at=%s",
                    channel_id,
                    stale_id,
                    stale.get("started_at"),
                )
                await stores.mongodb.complete_sync_job(
                    job_id=stale_id,
                    status="failed",
                    errors=["stale_recovery: job stuck in running state"],
                    failed_stage="stale_recovery",
                )

        # 1. Guard: no concurrent sync for the same channel.
        existing = await stores.mongodb.get_sync_status(channel_id)
        if existing is not None and existing.status == "running":
            if self._is_task_active(channel_id):
                raise ValueError(
                    f"Sync already running for channel {channel_id} (job_id={existing.id})."
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
                errors=["Recovered stale running job after process restart; safe to retry sync."],
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

        # 2b. Resolve effective policy for max_messages limit.
        from beever_atlas.services.policy_resolver import resolve_effective_policy

        effective_policy = await resolve_effective_policy(channel_id)
        _max_messages = effective_policy.sync.max_messages

        # 3. Resolve connection and fetch all messages via cursor-based pagination.
        resolved_connection_id = await self._resolve_connection_id(channel_id, connection_id)

        # File-imported channels: load persisted rows from imported_messages
        # instead of calling the bridge (there is no upstream platform).
        if resolved_connection_id is not None:
            resolved_conn = await stores.platform.get_connection(resolved_connection_id)
            if resolved_conn is not None and resolved_conn.platform == "file":
                return await self._start_file_sync(
                    channel_id=channel_id,
                    connection_id=resolved_connection_id,
                    since=since,
                    use_batch_api=use_batch_api,
                    resolved_type=resolved_type,
                    owner_principal_id=owner_principal_id,
                )

        # 3b. Create a placeholder sync job row IMMEDIATELY so the API
        # caller can return ``job_id`` without waiting on the bridge.
        # The bridge fetch (history pages + thread replies + channel
        # info) typically takes 5-30s for non-trivial channels —
        # the frontend's HTTP timeout is shorter than that and would
        # otherwise surface a misleading "Sync failed: Request timed
        # out" banner while the sync was, in fact, healthy.
        #
        # ``total_messages`` and ``parent_messages`` start at 0 and are
        # patched by ``_fetch_then_run`` once the fetch returns the real
        # counts. The frontend already polls /sync/status, so it sees
        # the totals appear within a few seconds.
        job = await stores.mongodb.create_sync_job(
            channel_id=channel_id,
            sync_type=resolved_type,
            total_messages=0,
            parent_messages=0,
            batch_size=settings.sync_batch_size,
            owner_principal_id=owner_principal_id,
            kind="sync",
        )
        job_id: str = job.id

        logger.info(
            "SyncRunner: queued %s sync for channel %s — job_id=%s; bridge fetch runs in background.",
            resolved_type,
            channel_id,
            job_id,
        )

        # 4. Launch the fetch+run pipeline as a background task and
        #    track it so concurrent ``start_sync`` calls for the same
        #    channel can be rejected.
        task = asyncio.create_task(
            self._fetch_then_run(
                job_id=job_id,
                channel_id=channel_id,
                resolved_connection_id=resolved_connection_id,
                resolved_type=resolved_type,
                since=since,
                max_messages=_max_messages,
                use_batch_api=use_batch_api,
            )
        )
        self._active_tasks[channel_id] = task

        return job_id

    async def _fetch_then_run(
        self,
        *,
        job_id: str,
        channel_id: str,
        resolved_connection_id: str | None,
        resolved_type: str,
        since: datetime | str | None,
        max_messages: int | None,
        use_batch_api: bool,
    ) -> None:
        """Fetch bridge messages then run the pipeline (background task).

        Split out from ``start_sync`` so the API endpoint returns the
        ``job_id`` without blocking on the bridge — see the comment in
        ``start_sync`` for the full rationale.

        Errors here mark the job as ``failed`` so the UI surfaces a real
        failure instead of an indefinite "running" state. The exception
        is swallowed because this runs as an asyncio Task; re-raising
        would land in the loop's default exception handler with a stack
        trace but no UI signal.
        """
        stores = get_stores()
        try:
            adapter = (
                ChatBridgeAdapter(connection_id=resolved_connection_id)
                if resolved_connection_id
                else get_adapter()
            )
            try:
                logger.info(
                    "SyncRunner: fetch start channel=%s connection_id=%s resolved_type=%s since=%s max_messages=%s",
                    channel_id,
                    resolved_connection_id,
                    resolved_type,
                    since,
                    max_messages,
                )
                messages = await self._fetch_all_messages(
                    channel_id, adapter=adapter, since=since, max_messages=max_messages
                )

                # If incremental sync found nothing, auto-fallback to full sync
                if not messages and resolved_type == "incremental":
                    logger.info(
                        "SyncRunner: incremental sync found no new messages for channel %s, falling back to full sync.",
                        channel_id,
                    )
                    resolved_type = "full"
                    messages = await self._fetch_all_messages(
                        channel_id, adapter=adapter, since=None, max_messages=max_messages
                    )

                if not messages:
                    logger.info(
                        "SyncRunner: no messages for channel %s (%s sync).",
                        channel_id,
                        resolved_type,
                    )

                # Fetch thread replies for messages with reply_count > 0.
                parent_count = len(messages)
                messages = await self._fetch_thread_replies(channel_id, messages, adapter=adapter)

                # Resolve human-readable channel name.
                channel_info = await adapter.get_channel_info(channel_id)
                channel_name = channel_info.name
            finally:
                if isinstance(adapter, ChatBridgeAdapter):
                    await adapter.close()

            # Patch the placeholder job row with the actual totals + the
            # final sync_type (which may have been promoted from
            # ``incremental`` to ``full`` above).
            # Global ``total_batches`` is locked in here so the user-facing
            # sync_jobs row exposes a stable denominator (29 for 711 msgs at
            # batch_size=25) instead of the per-tick worker totals (which
            # swing 5/7/4 as ticks claim varying counts of pending rows).
            #
            # Use ``batch_max_messages`` (the AdaptiveBatcher hard-cap,
            # default 30) — NOT ``sync_batch_size`` (default 50) — because
            # AdaptiveBatcher closes batches at the token-aware boundary,
            # which in practice hits the 30-msg cap before the input-token
            # budget. Estimating with the 50-msg ``sync_batch_size`` gave
            # the UI "15 batches" when reality was 24+, so total_batches
            # kept growing tick-by-tick (15 → 17 → 19 → 21 → 22 → 24)
            # as ``increment_batches_completed_for_channel`` used $max to
            # raise the denominator. Now the initial estimate is close to
            # actual, and the only remaining growth comes from edge cases
            # where the LLM trigger AdaptiveBatcher to use smaller batches.
            _settings = get_settings()
            # Pre-run the AdaptiveBatcher on the FULL message list to get
            # the EXACT batch count up-front. Previously we used a fixed
            # ``ceil(total / batch_max_messages)`` estimate which under-
            # counts when thread groups force token-aware splits — the
            # UI denominator then crept upward (24 → 25) via the $max
            # update path. Running the batcher once here pre-computes
            # the same partition the worker would, modulo a small
            # ~5-10% drift from across-tick thread fragmentation. We
            # add a 10% buffer for that drift so total_batches stays
            # at or above the actual final count.
            try:
                if _settings.batch_max_prompt_tokens > 0:
                    from beever_atlas.services.adaptive_batcher import token_aware_batches

                    _projected = token_aware_batches(
                        [m if isinstance(m, dict) else vars(m) for m in messages],
                        max_tokens=_settings.batch_max_prompt_tokens,
                        time_window_seconds=_settings.batch_time_window_seconds,
                        max_output_tokens=(
                            _settings.batch_max_output_tokens
                            if _settings.batch_max_output_tokens > 0
                            else None
                        ),
                        max_facts_per_message=_settings.max_facts_per_message,
                        max_messages=_settings.batch_max_messages,
                    )
                    _projected_count = max(1, len(_projected))
                    # 10% drift buffer for thread fragmentation across ticks.
                    _global_total_batches = max(
                        _projected_count,
                        int(_projected_count * 1.10) + 1,
                    )
                else:
                    _bsize = max(1, _settings.batch_max_messages)
                    _global_total_batches = (len(messages) + _bsize - 1) // _bsize
            except Exception:
                # If the pre-run fails for any reason (malformed messages,
                # import path drift), fall back to the simple ceiling
                # estimate. Better a slightly-off denominator than a sync
                # that won't start.
                logger.warning(
                    "SyncRunner: AdaptiveBatcher pre-run failed — falling back to fixed estimate",
                    exc_info=True,
                )
                _bsize = max(1, _settings.batch_max_messages)
                _global_total_batches = (len(messages) + _bsize - 1) // _bsize
            await stores.mongodb.set_sync_job_totals(
                job_id=job_id,
                total_messages=len(messages),
                parent_messages=parent_count,
                sync_type=resolved_type,
                total_batches=_global_total_batches,
            )

            logger.info(
                "SyncRunner: started %s sync for channel %s — job_id=%s, %d messages to process.",
                resolved_type,
                channel_id,
                job_id,
                len(messages),
            )

            await self._run_sync(
                job_id=job_id,
                channel_id=channel_id,
                channel_name=channel_name,
                messages=messages,
                parent_count=parent_count,
                sync_type=resolved_type,
                use_batch_api=use_batch_api,
            )
        except Exception as exc:  # noqa: BLE001 — background task: catch-all is the contract
            logger.exception(
                "SyncRunner._fetch_then_run failed job_id=%s channel=%s",
                job_id,
                channel_id,
            )
            try:
                await stores.mongodb.complete_sync_job(
                    job_id=job_id,
                    status="failed",
                    errors=[f"fetch_or_run_failed: {exc!s}"],
                    failed_stage="fetch",
                )
            except Exception:
                logger.exception("SyncRunner: also failed to mark job %s as failed", job_id)

    async def _fetch_all_messages(
        self,
        channel_id: str,
        adapter: Any,
        since: datetime | str | None = None,
        max_messages: int | None = None,
    ) -> list[Any]:
        """Fetch all messages via cursor-based pagination.

        The bridge adapter caps each page at 500 messages. We continue until
        we hit *max_messages* (or ``settings.sync_max_messages``) or the adapter returns nothing.

        Args:
            channel_id: Channel to fetch from.
            since: Timestamp cursor for incremental fetches (``None`` for full).

        Returns:
            Flat list of NormalizedMessage objects.
        """
        settings = get_settings()
        msg_limit = max_messages or settings.sync_max_messages
        all_messages: list[Any] = []
        seen_ids: set[str] = set()
        cursor = _coerce_since_timestamp(since)

        while len(all_messages) < msg_limit:
            page_num = (len(all_messages) // 500) + 1
            # Use order=asc so that `since` (Slack's `oldest`) cursor moves
            # forward chronologically, avoiding duplicate re-fetches.
            batch = await adapter.fetch_history(
                channel_id,
                since=cursor,
                limit=500,
                order="asc",
            )
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
                batch = [m for m in batch if getattr(m, "timestamp", None) and m.timestamp > cursor]
            if not batch:
                logger.info(
                    "SyncRunner: fetch page=%d channel=%s had no newer rows; stopping.",
                    page_num,
                    channel_id,
                )
                break

            # Deduplicate by message_id as a safety net.
            deduped_batch: list[Any] = []
            for m in batch:
                mid = getattr(m, "message_id", "") or getattr(m, "ts", "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                deduped_batch.append(m)
            batch = deduped_batch

            if not batch:
                break

            remaining = msg_limit - len(all_messages)
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
        adapter: Any,
    ) -> list[Any]:
        """Fetch thread replies for messages with reply_count > 0.

        Inserts replies adjacent to their parent so the batch processor
        groups them in the same batch for thread context resolution.

        Uses a semaphore to respect Slack API rate limits (Tier 3).
        """
        sem = asyncio.Semaphore(3)

        # Identify thread parents
        thread_parents = [m for m in messages if getattr(m, "reply_count", 0) > 0]

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
                    replies = [r for r in replies if getattr(r, "message_id", "") != thread_id]
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

    async def _resolve_connection_id(
        self,
        channel_id: str,
        explicit_connection_id: str | None,
    ) -> str | None:
        """Resolve which platform connection should be used for channel fetches.

        Priority order:
          1. Explicit connection_id from the caller.
          2. Connections whose ``selected_channels`` contains the channel id —
             the opt-in path. Tiebreaks alphabetically on id.
          3. Generic bridge probe — ask each connected adapter "do you know
             this channel?" and pick the first that responds successfully.
             Covers channels synced via "explore + click sync" (no explicit
             selection list entry), and channels whose sync_state was wiped
             by ``/api/admin/channels/{id}/reset``. Same pattern used by
             ``api/channels.get_channel`` for direct-URL navigation —
             generic, no platform-format heuristics.
        """
        if explicit_connection_id:
            return explicit_connection_id

        stores = get_stores()
        connections = await stores.platform.list_connections()
        connected = [c for c in connections if c.status == "connected"]

        # Path 2: explicit selected_channels membership.
        selected_candidates = [c for c in connected if channel_id in (c.selected_channels or [])]
        if len(selected_candidates) == 1:
            return selected_candidates[0].id
        if len(selected_candidates) > 1:
            # Deterministic fallback for duplicate channel IDs across
            # explicitly-selected connections.
            selected = sorted(selected_candidates, key=lambda c: c.id)[0]
            logger.warning(
                "SyncRunner: channel %s is selected in %d connections; defaulting to connection %s",
                channel_id,
                len(selected_candidates),
                selected.id,
            )
            return selected.id

        # Path 3: generic bridge probe across every connected connection.
        # Whichever adapter's bridge can fetch channel info owns the channel
        # — works for any platform, no format heuristics.
        try:
            from beever_atlas.adapters.bridge import BridgeError
            from beever_atlas.services.channel_discovery import make_bridge_adapter
        except Exception:  # noqa: BLE001
            return None

        for conn in connected:
            adapter = make_bridge_adapter(conn.id)
            try:
                await adapter.get_channel_info(channel_id)
                logger.info(
                    "SyncRunner: bridge probe matched channel %s to connection %s (platform=%s)",
                    channel_id,
                    conn.id,
                    conn.platform,
                )
                return conn.id
            except (KeyError, BridgeError):
                continue
            except Exception:  # noqa: BLE001
                continue
            finally:
                try:
                    await adapter.close()
                except Exception:  # noqa: BLE001
                    pass

        return None

    async def _start_file_sync(
        self,
        channel_id: str,
        connection_id: str,
        since: datetime | str | None,
        use_batch_api: bool,
        resolved_type: str,
        owner_principal_id: str | None = None,
    ) -> str:
        """Start a sync for a file-imported channel.

        Messages live in the ``imported_messages`` MongoDB collection — no
        bridge involvement. The rest of the pipeline (BatchProcessor,
        consolidation policy) is reused unchanged so file channels behave
        identically to platform channels from the downstream POV.
        """
        from beever_atlas.adapters.base import NormalizedMessage

        stores = get_stores()
        settings = get_settings()

        since_dt = _coerce_since_timestamp(since) if since else None

        query: dict[str, Any] = {"channel_id": channel_id}
        if since_dt is not None:
            query["timestamp"] = {"$gt": since_dt}

        raw_docs: list[dict[str, Any]] = []
        cursor = stores.mongodb.db["imported_messages"].find(query).sort("timestamp", 1)
        async for doc in cursor:
            raw_docs.append(doc)

        messages: list[NormalizedMessage] = []
        channel_name = channel_id
        for doc in raw_docs:
            ts = doc.get("timestamp")
            if not isinstance(ts, datetime):
                try:
                    ts = datetime.fromisoformat(
                        str(doc.get("timestamp_iso", "")).replace("Z", "+00:00")
                    )
                except ValueError:
                    continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            messages.append(
                NormalizedMessage(
                    content=doc.get("content", ""),
                    author=doc.get("author", ""),
                    platform="file",
                    channel_id=channel_id,
                    channel_name=doc.get("channel_name", channel_id),
                    message_id=doc.get("message_id", ""),
                    timestamp=ts,
                    thread_id=doc.get("thread_id"),
                    attachments=doc.get("attachments", []),
                    reactions=doc.get("reactions", []),
                    reply_count=doc.get("reply_count", 0),
                    raw_metadata={"source": "file_import"},
                    author_name=doc.get("author_name", ""),
                    author_image=doc.get("author_image", "") or "",
                )
            )
            if doc.get("channel_name"):
                channel_name = doc["channel_name"]

        parent_count = len(messages)
        logger.info(
            "SyncRunner: file sync channel=%s type=%s messages=%d",
            channel_id,
            resolved_type,
            parent_count,
        )

        job = await stores.mongodb.create_sync_job(
            channel_id=channel_id,
            sync_type=resolved_type,
            total_messages=len(messages),
            parent_messages=parent_count,
            batch_size=settings.sync_batch_size,
            owner_principal_id=owner_principal_id,
            kind="sync",
        )

        task = asyncio.create_task(
            self._run_sync(
                job_id=job.id,
                channel_id=channel_id,
                channel_name=channel_name,
                messages=messages,
                parent_count=parent_count,
                sync_type=resolved_type,
                use_batch_api=use_batch_api,
            )
        )
        self._active_tasks[channel_id] = task
        _ = connection_id  # kept for symmetry with platform path
        return job.id

    async def _run_sync(
        self,
        job_id: str,
        channel_id: str,
        channel_name: str,
        messages: list[Any],
        parent_count: int = 0,
        sync_type: str = "full",
        use_batch_api: bool = False,
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
            # Resolve per-channel ingestion config from policy
            from beever_atlas.services.policy_resolver import resolve_effective_policy

            effective_policy = await resolve_effective_policy(channel_id)

            # Persist messages into the durable channel_messages collection
            # BEFORE LLM extraction. The store is the source of truth from the
            # moment a message is fetched — extraction failures (e.g. Gemini
            # 503) no longer make the message disappear, and the background
            # ExtractionWorker consumes pending rows from here.
            #
            # Best-effort: a Mongo-side failure logs a WARN and the sync
            # continues with inline extraction (existing behaviour). The
            # READ_FROM_MESSAGE_STORE flag is what makes UI reads depend on the
            # upsert succeeding; nothing here blocks on it yet.
            #
            # Capture ``inserted_count`` so the cursor-advance branch
            # increments ``total_synced_messages`` by NEW rows only, avoiding
            # inflation on a manual re-sync that re-fetches already-stored
            # messages.
            inserted_count: int | None = None
            if messages:
                try:
                    cm_rows = _normalized_to_channel_messages(messages)
                    if cm_rows:
                        upsert_result = await stores.mongodb.upsert_channel_messages(cm_rows)
                        inserted_count = int(upsert_result.get("inserted", 0))
                        logger.info(
                            "SyncRunner: channel_messages upsert job_id=%s channel=%s "
                            "inserted=%d matched=%d modified=%d",
                            job_id,
                            channel_id,
                            inserted_count,
                            upsert_result.get("matched", 0),
                            upsert_result.get("modified", 0),
                        )
                        # memory-then-wiki-pipeline-realignment — kick the
                        # extraction worker so the first claim fires now
                        # instead of waiting for the next 10s tick boundary.
                        # No-op when DECOUPLE_EXTRACTION=false (inline path)
                        # or when the worker is not yet registered.
                        if inserted_count > 0:
                            try:
                                from beever_atlas.services.extraction_worker import (
                                    get_extraction_worker,
                                )

                                _worker = get_extraction_worker()
                                if _worker is not None:
                                    _worker.kick()
                            except Exception:  # noqa: BLE001 — best-effort
                                logger.debug(
                                    "SyncRunner: extraction worker kick failed",
                                    exc_info=True,
                                )
                except Exception as exc:  # noqa: BLE001 — additive store
                    logger.warning(
                        "SyncRunner: channel_messages upsert failed job_id=%s "
                        "channel=%s err=%s — sync continues without store; "
                        "dual-read fallback will keep UI working",
                        job_id,
                        channel_id,
                        exc,
                    )

            # With DECOUPLE_EXTRACTION ON, sync skips inline extraction
            # entirely — messages already landed in ``channel_messages`` with
            # ``extraction_status="pending"`` via ``$setOnInsert``, so the
            # background ExtractionWorker picks them up in the next tick
            # (default 30 s). Sync returns in seconds; a Gemini 503 can no
            # longer kill the job. Rolls back trivially: flag OFF returns to
            # inline extraction.
            decouple_extraction = get_settings().decouple_extraction
            if decouple_extraction:
                logger.info(
                    "SyncRunner: DECOUPLE_EXTRACTION=true — skipping inline "
                    "extraction job_id=%s channel=%s message_count=%d "
                    "(worker will claim from channel_messages)",
                    job_id,
                    channel_id,
                    len(messages),
                )
                # Synthesize an empty BatchResult so downstream cursor + status
                # logic is unchanged. The worker will populate per-row state.
                from beever_atlas.services.batch_processor import BatchResult

                result = BatchResult()
            else:
                result = await self._batch_processor.process_messages(
                    messages=messages,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    sync_job_id=job_id,
                    ingestion_config=effective_policy.ingestion,
                    use_batch_api=use_batch_api,
                )

            # Determine last_sync_ts from the latest TOP-LEVEL message only.
            # Thread replies may have older timestamps that would cause cursor drift.
            last_ts: str | None = None
            if messages:
                top_level = [
                    m
                    for m in messages
                    if not getattr(m, "thread_id", None)
                    or getattr(m, "thread_id", None) == getattr(m, "message_id", "")
                ]
                if top_level:
                    timestamps = [
                        getattr(m, "timestamp", None)
                        for m in top_level
                        if getattr(m, "timestamp", None) is not None
                    ]
                    if timestamps:
                        max_ts = max(timestamps)
                        last_ts = (
                            max_ts.isoformat() if hasattr(max_ts, "isoformat") else str(max_ts)
                        )

            # Mark job complete.
            # Three terminal states replace the prior all-or-nothing model.
            # ``completed_with_errors`` lets the cursor advance even when some
            # batches fail, so successful batches are not discarded by a Gemini 503.
            sync_status = "completed" if not result.errors else "completed_with_errors"
            sync_errors = None
            failed_stage = None
            failed_batches: list[dict[str, Any]] = []
            if result.errors:
                sync_errors = [
                    f"batch={err.get('batch_num')} error={err.get('error')}"
                    for err in result.errors
                ]
                # Record the last failed batch as the failed stage for the UI
                # (preserved for backward compat — UI may still surface this).
                last_err = result.errors[-1]
                failed_stage = f"Failed at batch {last_err.get('batch_num')}: {last_err.get('error', 'unknown error')}"

                # Build structured per-batch diagnostics and emit a WARN log
                # per failed batch so operators can trace and re-sync if needed.
                # Cross-reference batch_breakdowns for duration / counts where present.
                breakdown_by_idx = {bb.batch_num: bb for bb in result.batch_breakdowns}
                for err in result.errors:
                    batch_idx = err.get("batch_num")
                    err_str = str(err.get("error", "unknown"))
                    err_class = (
                        err.get("error_class") or type(err.get("error_obj", Exception())).__name__
                    )
                    bb = breakdown_by_idx.get(batch_idx) if batch_idx is not None else None
                    entry = {
                        "batch_index": batch_idx,
                        "message_count": err.get("message_count"),
                        "error_class": err_class,
                        "error_summary": err_str[:500],
                        "timestamp_range_start": err.get("timestamp_range_start"),
                        "timestamp_range_end": err.get("timestamp_range_end"),
                        "duration_seconds": (bb.duration_seconds if bb else None),
                    }
                    failed_batches.append(entry)
                    logger.warning(
                        "SyncRunner: failed_batch job_id=%s channel=%s batch=%s msgs=%s err_class=%s err=%.200s",
                        job_id,
                        channel_id,
                        batch_idx,
                        entry["message_count"],
                        err_class,
                        err_str,
                        extra={
                            "event": "failed_batch",
                            "sync_job_id": job_id,
                            "channel_id": channel_id,
                            "batch_index": batch_idx,
                            "message_count": entry["message_count"],
                            "error_class": err_class,
                            "error_summary": entry["error_summary"],
                            "timestamp_range_start": entry["timestamp_range_start"],
                            "timestamp_range_end": entry["timestamp_range_end"],
                        },
                    )
            await stores.mongodb.complete_sync_job(
                job_id=job_id,
                status=sync_status,
                errors=sync_errors,
                failed_stage=failed_stage,
                failed_batches=failed_batches if failed_batches else None,
            )

            # Cursor advances on successful fetch regardless of extraction
            # outcome. Successful batches are no longer discarded when sibling
            # batches fail — the per-batch ``failed_batches`` diagnostic above is
            # the trace for any messages that need manual recovery.
            if last_ts is not None:
                # For incremental syncs, increment ``total_synced_messages``
                # by the upsert's NEW-rows count — not by ``parent_count``,
                # which double-counts on a re-sync that re-fetches messages
                # already in the store. Falls back to ``parent_count`` only
                # when the upsert was skipped (no messages) or failed
                # (best-effort path).
                if sync_type == "incremental":
                    increment = inserted_count if inserted_count is not None else parent_count
                    await stores.mongodb.update_channel_sync_state(
                        channel_id=channel_id,
                        last_sync_ts=last_ts,
                        increment=increment,
                    )
                else:
                    # Full syncs SET the total (replacing the old count) so
                    # `parent_count` remains the right value here regardless
                    # of upsert deltas.
                    await stores.mongodb.update_channel_sync_state(
                        channel_id=channel_id,
                        last_sync_ts=last_ts,
                        set_total=parent_count,
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
                    "total_messages": parent_count or len(messages),
                    "error_count": len(result.errors),
                    "results_summary": batch_summaries,
                },
            )

            # Trigger consolidation via pipeline orchestrator (policy-aware).
            # When DECOUPLE_EXTRACTION=true, facts=0 at sync-return time (the
            # background ExtractionWorker hasn't run yet). Firing consolidation
            # here would see an empty Weaviate and produce 0 clusters. Instead,
            # the ExtractionWorker's on_extraction_done subscriber (wired in
            # server/app.py) fires consolidation after each successful batch so
            # it sees the real facts.
            if not result.errors and not decouple_extraction:
                from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete

                await on_ingestion_complete(channel_id, result.total_facts)

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
                failed_stage=f"Pipeline error: {str(exc)[:200]}",
            )

            await stores.mongodb.log_activity(
                event_type="sync_failed",
                channel_id=channel_id,
                details={"job_id": job_id, "channel_name": channel_name, "error": str(exc)},
            )

        finally:
            self._active_tasks.pop(channel_id, None)

    async def cancel_sync(self, channel_id: str) -> bool:
        """Cancel the in-flight sync task for ``channel_id`` (process-local).

        Scoped mirror of :meth:`shutdown` for a single channel — used by the
        Wave-2 purge service as best-effort cleanup BEFORE the durable lock +
        writer guards take over (the lock is the cross-process guarantee;
        this just stops a same-process task sooner). Cancels the task, awaits
        it, swallows ``CancelledError``, and pops it from the registry.

        Returns:
            True if a live task was cancelled, False if none was active.

        EE caveat: only cancels tasks in THIS process. In a multi-worker
        deployment a sync running in another worker is stopped by the lock +
        guards, not by this call.
        """
        task = self._active_tasks.get(channel_id)
        if task is None or task.done():
            self._active_tasks.pop(channel_id, None)
            return False
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — log, don't propagate
            logger.warning(
                "SyncRunner: task raised during cancel_sync channel=%s: %s",
                channel_id,
                exc,
            )
        self._active_tasks.pop(channel_id, None)
        return True

    async def shutdown(self) -> None:
        """Cancel all active sync and consolidation tasks gracefully."""
        from beever_atlas.services.pipeline_orchestrator import get_active_consolidation_tasks

        consolidation_tasks = get_active_consolidation_tasks()
        if not self._active_tasks and not consolidation_tasks:
            return

        logger.info(
            "SyncRunner: shutting down — cancelling %d active task(s).",
            len(self._active_tasks) + len(consolidation_tasks),
        )

        tasks = list(self._active_tasks.values()) + list(consolidation_tasks.values())
        for task in tasks:
            task.cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)
        for task, res in zip(tasks, results):
            if isinstance(res, Exception) and not isinstance(res, asyncio.CancelledError):
                logger.warning("SyncRunner: task raised during shutdown: %s", res)

        self._active_tasks.clear()
        consolidation_tasks.clear()
        logger.info("SyncRunner: shutdown complete.")
