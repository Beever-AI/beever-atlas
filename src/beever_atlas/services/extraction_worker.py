"""Background extraction worker.

Drains pending messages from the durable Message Store and feeds them
through the existing ``BatchProcessor`` so sync (fetch + persist) is
decoupled from extraction (LLM calls). When the ``DECOUPLE_EXTRACTION``
flag is ON, sync writes ``ChannelMessage`` rows with
``extraction_status="pending"`` and returns; this worker — registered as
periodic APScheduler jobs by :class:`SyncScheduler` — claims them
atomically via ``find_one_and_update``, runs extraction, and bulk-updates
status to ``done`` or ``failed``.

Spec: ``openspec/changes/oss-pipeline-and-wiki-redesign/specs/extraction-worker/``
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from beever_atlas.adapters.base import NormalizedMessage
from beever_atlas.services.batch_processor import BatchProcessor
from beever_atlas.services.pipeline_events import get_pipeline_events

if TYPE_CHECKING:
    from beever_atlas.services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


ExtractionDoneCallback = Callable[[str, list[str]], Awaitable[None] | None]
"""DEPRECATED. Signature for ``on_extraction_done`` subscribers.

Kept as a transitional alias during the ``memory-then-wiki-pipeline-realignment``
deprecation window. New code should subscribe via
:meth:`ExtractionWorker.subscribe_memory_changed` (accumulator) +
:meth:`ExtractionWorker.subscribe_memory_settled` (terminal trigger).
"""

MemoryChangedCallback = Callable[[str, list[str]], Awaitable[None] | None]
"""``memory_changed`` subscriber signature ``(channel_id, fact_ids)``.

Accumulator-only — handlers route facts into durable per-page accumulators
and do NOT take terminal actions (LLM rewrites, builder runs). Terminal
actions wait for the corresponding :data:`MemorySettledCallback`.
"""

MemorySettledCallback = Callable[[str], Awaitable[None] | None]
"""``memory_settled`` subscriber signature ``(channel_id,)``.

Fired ONLY when the channel's extraction queue transitions to empty
(``pending+extracting=0``). Idempotent — multiple emits for the same
drained channel are safe. Subscribers take terminal action on this
event (flush wiki dirty queue, run auto-overview, etc.).
"""


def _doc_to_normalized_message(doc: dict[str, Any]) -> NormalizedMessage | None:
    """Reverse of ``_normalized_to_channel_messages`` for the worker path.

    Constructs a ``NormalizedMessage`` from a ``channel_messages`` row so
    the existing ``BatchProcessor.process_messages`` consumer needs no
    changes. Returns None on malformed rows so the worker can skip them
    without poisoning the rest of the batch.
    """
    try:
        timestamp = doc.get("timestamp")
        if isinstance(timestamp, str):
            timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        if timestamp is None:
            return None
        if not isinstance(timestamp, datetime):
            return None
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        # ``platform`` is the runtime-facing label the BatchProcessor /
        # ADK pipeline reads; for pull adapters it equals ``source_id``,
        # for push receivers we record the registered source_id verbatim
        # (e.g. ``openclaw-prod``) so downstream provenance is faithful.
        platform = str(doc.get("source_id") or "unknown")
        return NormalizedMessage(
            content=str(doc.get("content") or ""),
            author=str(doc.get("author") or ""),
            platform=platform,
            channel_id=str(doc.get("channel_id") or ""),
            channel_name=str(doc.get("channel_name") or ""),
            message_id=str(doc.get("message_id") or ""),
            timestamp=timestamp,
            thread_id=doc.get("thread_id"),
            attachments=list(doc.get("attachments") or []),
            reactions=list(doc.get("reactions") or []),
            reply_count=int(doc.get("reply_count") or 0),
            raw_metadata=dict(doc.get("raw_metadata") or {}),
            author_name=str(doc.get("author_name") or ""),
            author_image=str(doc.get("author_image") or ""),
            # Discord-only guild id persisted on the channel_messages row;
            # carried back so it survives into preprocessed_messages and the
            # persister can stamp it onto the fact for permalink construction.
            guild_id=str(doc.get("guild_id") or ""),
        )
    except Exception as exc:  # noqa: BLE001 — defensive: skip the bad row
        logger.warning(
            "ExtractionWorker: skipped malformed channel_messages row "
            "channel=%s message_id=%s exc=%s: %.200s",
            doc.get("channel_id"),
            doc.get("message_id"),
            type(exc).__name__,
            str(exc),
        )
        return None


_RETRY_BACKOFF_SCHEDULE: list[int] = [5, 30, 90, 180, 360]
"""Exponential backoff per attempt (seconds). Capped at the tail
for attempts beyond the schedule. First retry intentionally short (5s)
for transient hiccups; later attempts climb sharply for real
rate-limit windows. Mirrors ``batch_processor._LLM_RETRY_BACKOFF``
(commit 84c4413) — the two schedules must move together; if one
changes, change the other. (Architect-agent audit caught the drift
between this file and batch_processor.py — both now aligned.)"""

_MAX_RETRIES: int = len(_RETRY_BACKOFF_SCHEDULE)
"""Max retry attempts before a failed row stays failed permanently.
Tied to the backoff schedule length so the two cannot drift."""

_TICK_SECONDS: int = 10
"""Default scheduler tick interval. Reduced from 30s to 10s in
``memory-then-wiki-pipeline-realignment`` — combined with the
``kick()`` event channel, the worker now responds to new pending
rows within 1-2 seconds typically; the tick is the safety floor."""

_STALE_SECONDS: int = 600
"""Stale-extracting recovery threshold. A row stuck in ``extracting``
longer than this is reset to ``pending`` (worker crash recovery).
10 min is conservative — extraction batches typically take 30-90s."""


def _retry_backoff_seconds(attempt_count: int) -> int:
    """Look up the backoff seconds for a given attempt count.

    Monotonic exponential schedule capped at the tail.
    """
    if attempt_count <= 0:
        return _RETRY_BACKOFF_SCHEDULE[0]
    if attempt_count - 1 < len(_RETRY_BACKOFF_SCHEDULE):
        return _RETRY_BACKOFF_SCHEDULE[attempt_count - 1]
    return _RETRY_BACKOFF_SCHEDULE[-1]


class ExtractionWorker:
    """Background worker that drains the ``channel_messages`` extraction queue.

    The worker is intentionally stateless — every tick it claims a fresh
    batch and runs it independently. There is no in-memory queue. This
    makes restarts trivial (any crashed ``extracting`` row is reclaimed
    by the next ``sweep_stale`` after ``stale_seconds``).

    Concurrency: the worker owns a ``asyncio.Semaphore`` separate from
    sync's ``max_concurrent_syncs``. Sync rate (channels fetching
    concurrently) and extraction rate (batches hitting the LLM
    concurrently) are independent constraints — sharing one semaphore
    would couple unrelated rate limits (design D5).
    """

    def __init__(
        self,
        batch_processor: BatchProcessor | None = None,
        semaphore_size: int | None = None,
        settle_seconds: int = 2,
        stale_seconds: int = 600,
        breaker: "CircuitBreaker | None" = None,
    ) -> None:
        # Pass the same CircuitBreaker singleton into the BatchProcessor
        # so the worker path and the inline path share one breaker —
        # a 503 storm against the worker trips the same breaker that
        # the inline path observes.
        from beever_atlas.services.circuit_breaker import get_circuit_breaker

        breaker = breaker or get_circuit_breaker()
        self._breaker = breaker
        self._batch_processor = batch_processor or BatchProcessor(breaker=breaker)
        # Lazily initialised on first tick so the constructor stays
        # event-loop-free and importable from non-async test fixtures.
        self._semaphore_size = semaphore_size
        self._semaphore: asyncio.Semaphore | None = None
        self._settle_seconds = settle_seconds
        self._stale_seconds = stale_seconds
        self._on_extraction_done: list[ExtractionDoneCallback] = []
        # memory-then-wiki-pipeline-realignment — two-event contract.
        # ``memory_changed`` is the accumulator (fires per batch);
        # ``memory_settled`` is the terminal trigger (fires when the
        # channel's queue drains). See design.md D1.
        self._on_memory_changed: list[MemoryChangedCallback] = []
        self._on_memory_settled: list[MemorySettledCallback] = []
        # Channels whose queue was non-empty at the start of the
        # current tick; used to detect the "transitioned to empty"
        # edge and emit memory_settled exactly once per drain.
        self._channels_with_pending_pre_tick: set[str] = set()
        # memory-then-wiki-pipeline-realignment — kick channel.
        # SyncRunner sets this event after a sync upserts new pending
        # rows; the worker's run loop awaits it (with a tick-interval
        # timeout) so the first claim fires immediately instead of
        # waiting for the next 10s tick boundary. ``asyncio.Event`` is
        # idempotent — back-to-back sets coalesce into one wakeup.
        self._kick_event: asyncio.Event | None = None
        # Total kicks received since process start — surfaced in
        # ``metrics_snapshot`` so operators can verify SyncRunner is
        # actually kicking after upserts.
        self._kick_received_count: int = 0
        # Rolling-window metrics for the admin observability endpoint
        # (production-wiring §20). Each entry is a per-tick record:
        # ``(monotonic_ts, claimed, succeeded, failed)``. Trimmed to the
        # most recent ~10 minutes worth of ticks (well past the 60-min
        # window we summarise — a tick is at most every ``_TICK_SECONDS``,
        # so 10 min ≈ 20 entries).
        self._tick_records: list[tuple[float, int, int, int]] = []
        # Phase 0 / Task 1.2 — separate, capped ring used by the smoothed
        # ETA calculator (Phase 3). Holds ``(monotonic_ts, succeeded,
        # failed)`` for the most recent 60 ticks. Distinct from
        # ``_tick_records`` (which uses a 60-min wall-clock cutoff for
        # claim-rate maths) so the ETA window can be tuned independently.
        self._tick_samples: list[tuple[float, int, int]] = []
        # Most recent failed-row records (capped at 10) for the admin
        # endpoint's ``recent_failures`` field. Each entry is
        # ``{message_id, channel_id, error_class, ts}``.
        self._recent_failures: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Event subscription (used by WikiMaintainer)
    # ------------------------------------------------------------------

    def subscribe_extraction_done(self, callback: ExtractionDoneCallback) -> None:
        """DEPRECATED. Register a callback under the legacy combined event.

        Kept as a transitional alias during the
        ``memory-then-wiki-pipeline-realignment`` deprecation window. New
        consumers should call :meth:`subscribe_memory_changed` and/or
        :meth:`subscribe_memory_settled` directly.

        Internally the legacy callbacks are invoked from
        :meth:`_emit_extraction_done` which still fires per batch
        (accumulator semantics — terminal callers receive both legacy
        callback AND ``memory_settled`` events).
        """
        self._on_extraction_done.append(callback)

    def subscribe_memory_changed(self, callback: MemoryChangedCallback) -> None:
        """Register a callback fired AFTER every per-channel batch.

        Accumulator-only. Subscribers MUST NOT take terminal action
        (LLM calls, builder runs) on this event — route facts into
        durable per-page accumulators (see ``wiki_dirty_queue``) and
        wait for :meth:`subscribe_memory_settled`.
        """
        self._on_memory_changed.append(callback)

    def kick(self) -> None:
        """Wake the worker's run loop immediately.

        Called by ``SyncRunner`` (or any upstream producer) after new
        pending rows land in ``channel_messages``. Lazily initialises
        the ``asyncio.Event`` on first call so the constructor stays
        loop-free for non-async test fixtures. Multiple kicks coalesce
        into one wakeup — the run loop processes them in a single tick.
        """
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — kicks from sync code (e.g., tests) are
            # absorbed as a counter bump but won't wake an unstarted loop.
            self._kick_received_count += 1
            return
        if self._kick_event is None:
            self._kick_event = asyncio.Event()
        self._kick_event.set()
        self._kick_received_count += 1

    async def wait_for_kick(self, timeout: float) -> bool:
        """Await the next kick (or timeout). Returns True if kicked.

        Run loops call this with ``timeout=_TICK_SECONDS`` so the worker
        wakes on either the kick OR the tick floor. The event is
        cleared after wait returns so the next call blocks again.
        """
        if self._kick_event is None:
            self._kick_event = asyncio.Event()
        try:
            await asyncio.wait_for(self._kick_event.wait(), timeout=timeout)
            kicked = True
        except TimeoutError:
            kicked = False
        self._kick_event.clear()
        return kicked

    def subscribe_memory_settled(self, callback: MemorySettledCallback) -> None:
        """Register a callback fired when the channel's queue drains.

        Terminal trigger — subscribers take action (flush, build) on
        this event. Idempotent: re-fires if the channel re-enters and
        drains again.
        """
        self._on_memory_settled.append(callback)

    async def _safe_invoke(
        self,
        cb: Callable[..., Awaitable[None] | None],
        *args: Any,
    ) -> None:
        """Invoke ``cb`` swallowing exceptions so one bad subscriber
        cannot crash siblings or block the worker tick.

        Logs with structured context for observability.
        """
        try:
            result = cb(*args)
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 — one bad subscriber must not stall extraction
            logger.exception(
                "ExtractionWorker: subscriber raised cb=%s args_head=%s",
                getattr(cb, "__name__", repr(cb)),
                repr(args[:2]) if args else "()",
            )

    async def _emit_extraction_done(self, channel_id: str, fact_ids: list[str]) -> None:
        """DEPRECATED legacy emission.

        Invokes the legacy ``on_extraction_done`` subscribers via
        fire-and-forget tasks. ALSO fans out to ``memory_changed``
        subscribers so the new accumulator path receives a parallel
        notification during the deprecation window. ``memory_settled``
        is NOT emitted here — it fires exactly once per drain in
        :meth:`tick` so per-batch over-firing is impossible.
        """
        for cb in self._on_extraction_done:
            asyncio.create_task(self._safe_invoke(cb, channel_id, fact_ids))
        # Dual-emit so subscribers that have already migrated to
        # ``memory_changed`` get the new event AND legacy callers keep
        # working unchanged. Removed after the deprecation window.
        for cb in self._on_memory_changed:
            asyncio.create_task(self._safe_invoke(cb, channel_id, fact_ids))

    async def _emit_memory_changed(self, channel_id: str, fact_ids: list[str]) -> None:
        """Fire ``memory_changed`` to all subscribers via create_task.

        Each subscriber runs in its own task so the worker tick is
        never blocked on subscriber I/O. Exceptions are swallowed and
        logged per subscriber.
        """
        for cb in self._on_memory_changed:
            asyncio.create_task(self._safe_invoke(cb, channel_id, fact_ids))

    async def _emit_memory_settled(self, channel_id: str) -> None:
        """Fire ``memory_settled`` to all subscribers via create_task.

        Idempotent — terminal subscribers (maintainer flush, auto
        overview) are themselves idempotent.
        """
        for cb in self._on_memory_settled:
            asyncio.create_task(self._safe_invoke(cb, channel_id))

    # ------------------------------------------------------------------
    # Public lifecycle methods (called by SyncScheduler)
    # ------------------------------------------------------------------

    async def tick(self, channel_id: str | None = None) -> dict[str, int]:
        """Drain one round of pending extraction work.

        Returns a counters dict ``{claimed, succeeded, failed, channels}``
        for observability. Idempotent — running multiple ticks
        concurrently is safe; the atomic claim ensures each row is picked
        up exactly once. ``channel_id`` narrows the scan when set
        (used by manual "extract now" admin actions).
        """
        from beever_atlas.infra.config import get_settings
        from beever_atlas.stores import get_stores

        settings = get_settings()
        stores = get_stores()
        if self._semaphore is None:
            size = self._semaphore_size or settings.ingest_batch_concurrency
            self._semaphore = asyncio.Semaphore(size)

        # Claim a batch sized to match the configured concurrency.
        # ``ingest_batch_concurrency`` is the *number of in-flight LLM
        # batches* — the worker claims that many ``ChannelMessage`` rows
        # per tick and lets ``BatchProcessor`` re-batch them internally
        # (token-aware batcher) before fanning out.
        claim_size = settings.sync_batch_size * settings.ingest_batch_concurrency
        # delete-channel-v2 Wave 0 — fetch the active purge set ONCE per tick
        # and pass it into the claim. The periodic tick drains every channel
        # (channel_id=None), so without this a purge in flight would have its
        # rows re-claimed and re-extracted by the global drain. Best-effort:
        # a Mongo blip here must not stall extraction, so we fall back to an
        # empty set (the claim still excludes nothing — the durable lock +
        # per-stage idempotency in the Wave-2 service remain the backstop).
        try:
            purging_channel_ids = await stores.mongodb.get_purging_channel_ids()
        except Exception:  # noqa: BLE001 — guard fetch must not crash the tick
            logger.exception(
                "ExtractionWorker: get_purging_channel_ids failed — "
                "proceeding without purge filter this tick"
            )
            purging_channel_ids = set()
        claimed = await stores.mongodb.claim_pending_messages_for_extraction(
            batch_size=claim_size,
            channel_id=channel_id,
            settle_seconds=self._settle_seconds,
            max_retries=_MAX_RETRIES,
            purging_channel_ids=purging_channel_ids,
        )
        counters = {"claimed": len(claimed), "succeeded": 0, "failed": 0, "channels": 0}
        if not claimed:
            self._record_tick_metrics(counters)
            return counters

        # Group by channel_id — BatchProcessor is per-channel.
        by_channel: dict[str, list[dict[str, Any]]] = {}
        for doc in claimed:
            by_channel.setdefault(doc.get("channel_id", ""), []).append(doc)
        counters["channels"] = len(by_channel)

        # Phase 0 / Task 1.3 — pipeline event: queue fetch for each channel.
        try:
            for ch_id, docs in by_channel.items():
                get_pipeline_events().record(
                    channel_id=ch_id,
                    stage="fetch",
                    label=f"Claimed {len(docs)} pending rows",
                )
        except Exception:  # noqa: BLE001
            pass

        async def _process_channel(ch_id: str, docs: list[dict[str, Any]]) -> tuple[int, int]:
            assert self._semaphore is not None
            async with self._semaphore:
                return await self._process_channel_batch(ch_id, docs)

        # Use ``return_exceptions=True`` so a raise in one channel's
        # processing does NOT cancel siblings and orphan their
        # claimed-but-not-finalized rows. Each channel's success/failure
        # tally is returned and summed AFTER gather, so there is no
        # shared-mutation race on ``counters``. If a sibling itself crashes,
        # we mark its claimed rows as failed via the store so the
        # stale-sweep doesn't have to recover them later.
        results: list[tuple[int, int] | BaseException] = await asyncio.gather(
            *[_process_channel(ch, docs) for ch, docs in by_channel.items()],
            return_exceptions=True,
        )
        from beever_atlas.stores import get_stores as _get_stores

        stores_ref = _get_stores()
        for (ch_id, docs), result in zip(by_channel.items(), results, strict=True):
            if isinstance(result, BaseException):
                logger.exception(
                    "ExtractionWorker: channel processing crashed channel=%s "
                    "rows=%d (finalizing as failed to free the queue): %s",
                    ch_id,
                    len(docs),
                    result,
                    exc_info=result,
                )
                # Free the rows synchronously rather than waiting for the
                # 5-minute stale sweep — the worker is already alive, just
                # one channel's task crashed.
                keys_with_attempts = [
                    (
                        (
                            str(d.get("source_id") or ""),
                            str(d.get("channel_id") or ""),
                            str(d.get("message_id") or ""),
                        ),
                        int(d.get("attempt_count") or 0),
                    )
                    for d in docs
                ]
                try:
                    await self._finalize_failed(
                        stores_ref,
                        keys_with_attempts,
                        error=f"worker_task_crashed: {type(result).__name__}",
                    )
                except Exception:
                    logger.exception(
                        "ExtractionWorker: post-crash finalize_failed also "
                        "raised — rows will recover via stale_sweep"
                    )
                counters["failed"] += len(docs)
                continue
            ok, fail = result
            counters["succeeded"] += ok
            counters["failed"] += fail

        # Re-derive the user-facing sync_jobs row's progress from the
        # extraction-status counts on channel_messages. The decoupled
        # worker uses a synthetic sync_job_id so BatchProcessor's
        # ``update_sync_progress`` calls land on a row that nobody
        # reads — without this refresh, ``processed_messages`` stays
        # at 0 and ``current_stage`` stays empty for the entire run.
        # Done once per tick (not per batch) to bound mongo write load.
        for ch_id in by_channel.keys():
            try:
                await stores_ref.mongodb.refresh_sync_progress_for_channel(ch_id)
            except Exception:
                # Progress refresh is best-effort observability — never
                # let a transient mongo blip break the worker tick.
                logger.exception(
                    "ExtractionWorker: refresh_sync_progress_for_channel raised channel=%s",
                    ch_id,
                )

        logger.info(
            "ExtractionWorker: tick complete claimed=%d succeeded=%d failed=%d channels=%d",
            counters["claimed"],
            counters["succeeded"],
            counters["failed"],
            counters["channels"],
        )
        # memory-then-wiki-pipeline-realignment — settlement detection.
        # For each channel touched in this tick, check whether its queue
        # has transitioned to empty. If so, emit ``memory_settled``.
        # Idempotent — a channel that was empty before the tick gets no
        # event; one that drains DURING the tick fires exactly once. A
        # channel that re-enters pending mid-tick is correctly skipped
        # (pending+extracting>0) and the next tick will detect when it
        # actually drains.
        for ch_id in list(by_channel.keys()):
            try:
                counts = await stores_ref.mongodb.count_channel_messages_by_status(ch_id)
                pending = int(counts.get("pending", 0))
                extracting = int(counts.get("extracting", 0))
                if pending == 0 and extracting == 0:
                    await self._emit_memory_settled(ch_id)
            except Exception:  # noqa: BLE001 — settlement is best-effort observability
                logger.exception(
                    "ExtractionWorker: memory_settled detection failed channel=%s",
                    ch_id,
                )
        self._record_tick_metrics(counters)
        return counters

    def _record_tick_metrics(self, counters: dict[str, int]) -> None:
        """Record one tick result for the rolling-window metrics endpoint.

        Trims out-of-window entries on every record so the in-memory list
        stays bounded even if no admin call ever drains it. The window
        upper-bound is the longest reporting window we summarise (60min);
        anything older is dropped.
        """
        now = time.monotonic()
        self._tick_records.append(
            (
                now,
                counters.get("claimed", 0),
                counters.get("succeeded", 0),
                counters.get("failed", 0),
            )
        )
        cutoff = now - 60 * 60  # 60 minutes
        self._tick_records = [r for r in self._tick_records if r[0] >= cutoff]
        # Phase 0 / Task 1.2 — separate, count-bounded ring for the
        # smoothed-ETA calculator (Phase 3). 60 samples gives the EWMA
        # enough history to absorb a single retry burst without thrashing.
        self._tick_samples.append(
            (
                now,
                counters.get("succeeded", 0),
                counters.get("failed", 0),
            )
        )
        if len(self._tick_samples) > 60:
            self._tick_samples = self._tick_samples[-60:]

    def tick_samples_snapshot(self) -> list[tuple[float, int, int]]:
        """Return a copy of the recent tick samples for the ETA calculator.

        Each entry is ``(monotonic_ts, succeeded, failed)``. Most-recent
        last (append order). Returned as a fresh list so the caller can
        safely iterate without holding any lock against the worker.
        """
        return list(self._tick_samples)

    def tick_samples_for_eta(self) -> list[tuple[float, int]]:
        """Return ``(monotonic_ts, succeeded)`` pairs for the ETA calculator.

        Phase 3 / Task 4.1.2 — the smoothed ETA only counts successful
        claims toward throughput; failed rows will be retried and don't
        represent forward progress. The full triple lives behind
        :meth:`tick_samples_snapshot` for any caller that needs both.
        """
        return [(ts, succ) for (ts, succ, _fail) in self._tick_samples]

    def _record_failure(self, *, message_id: str, channel_id: str, error_class: str) -> None:
        """Record a per-row failure for the admin endpoint's recent_failures.

        Capped at 10 entries so a flapping channel cannot fill the buffer
        with thousands of identical failures.
        """
        self._recent_failures.append(
            {
                "message_id": message_id,
                "channel_id": channel_id,
                "error_class": error_class,
                "ts": int(time.time()),
            }
        )
        if len(self._recent_failures) > 10:
            self._recent_failures = self._recent_failures[-10:]

    def metrics_snapshot(self) -> dict[str, Any]:
        """Read-only snapshot for the admin observability endpoint.

        Computes:
          - ``claim_rate_5min`` / ``_15min`` / ``_60min``: rolling claim
            count divided by window size in seconds
          - ``success_rate_5min``: succeeded / (succeeded + failed) over
            the last 5 minutes; 1.0 when there were no rows in the window
          - ``breaker_state``: passes through the configured breaker's
            current state for the operator dashboard
          - ``recent_failures``: most-recent 10 per-row failure records
        """
        now = time.monotonic()

        def _within(window_seconds: int) -> list[tuple[float, int, int, int]]:
            cutoff = now - window_seconds
            return [r for r in self._tick_records if r[0] >= cutoff]

        def _claim_rate(window_seconds: int) -> float:
            window_records = _within(window_seconds)
            claimed = sum(r[1] for r in window_records)
            return round(claimed / window_seconds, 4) if window_seconds > 0 else 0.0

        last_5min = _within(5 * 60)
        succeeded_5min = sum(r[2] for r in last_5min)
        failed_5min = sum(r[3] for r in last_5min)
        denom = succeeded_5min + failed_5min
        success_rate = (succeeded_5min / denom) if denom else 1.0

        breaker_state = "unknown"
        try:
            breaker_state = self._breaker.state()
        except Exception:  # noqa: BLE001 — best-effort
            pass

        return {
            "claim_rate_5min": _claim_rate(5 * 60),
            "claim_rate_15min": _claim_rate(15 * 60),
            "claim_rate_60min": _claim_rate(60 * 60),
            "success_rate_5min": round(success_rate, 4),
            "breaker_state": breaker_state,
            "recent_failures": list(self._recent_failures),
            # memory-then-wiki-pipeline-realignment — kick counter so
            # operators can verify SyncRunner is calling ``kick()`` after
            # each new upsert. A value that stays at 0 during active sync
            # indicates a wiring regression.
            "kick_received_count": self._kick_received_count,
        }

    async def sweep_stale(self) -> int:
        """Reset rows stuck in ``"extracting"`` longer than ``stale_seconds``."""
        from beever_atlas.stores import get_stores

        stores = get_stores()
        return await stores.mongodb.sweep_stale_extracting(stale_seconds=self._stale_seconds)

    # ------------------------------------------------------------------
    # Internal: per-channel batch processing
    # ------------------------------------------------------------------

    async def _process_channel_batch(
        self, channel_id: str, claimed_docs: list[dict[str, Any]]
    ) -> tuple[int, int]:
        """Run extraction for one channel's claimed messages.

        Returns ``(succeeded_count, failed_count)``. On uncaught
        exception during ``process_messages`` the entire claimed batch is
        transitioned to ``failed`` (per-message error attribution requires
        per-row provenance from the BatchProcessor; that's a future
        enhancement). Per-row exponential backoff is applied based on each
        row's ``attempt_count``.
        """
        from beever_atlas.stores import get_stores

        stores = get_stores()
        normalized: list[NormalizedMessage] = []
        valid_keys: list[tuple[str, str, str]] = []
        keys_with_attempts: list[tuple[tuple[str, str, str], int]] = []
        channel_name = ""
        for doc in claimed_docs:
            nm = _doc_to_normalized_message(doc)
            if nm is None:
                continue
            normalized.append(nm)
            key = (
                str(doc.get("source_id") or ""),
                str(doc.get("channel_id") or ""),
                str(doc.get("message_id") or ""),
            )
            valid_keys.append(key)
            keys_with_attempts.append((key, int(doc.get("attempt_count") or 0)))
            if not channel_name:
                channel_name = str(doc.get("channel_name") or "")

        if not normalized:
            return 0, 0

        sync_job_id = f"worker:{channel_id}:{int(time.time() * 1000)}"
        # Read the user-facing sync_jobs row's ``batches_completed`` once
        # per tick to use as the global ``batch_index_offset``. Without
        # this, every worker tick restarts batch_idx at 1, so the UI's
        # batch chips churn (Batch 1 in tick A is different messages than
        # Batch 1 in tick B). The offset shifts this tick's internal
        # batches to global positions ``offset+1..offset+K``.
        try:
            batch_index_offset = await stores.mongodb.get_user_facing_batches_completed(channel_id)
        except Exception:
            logger.exception(
                "ExtractionWorker: failed to read batches_completed offset "
                "channel=%s — defaulting to 0",
                channel_id,
            )
            batch_index_offset = 0
        started = time.monotonic()
        try:
            result = await self._batch_processor.process_messages(
                messages=normalized,
                channel_id=channel_id,
                channel_name=channel_name,
                sync_job_id=sync_job_id,
                ingestion_config=None,
                batch_index_offset=batch_index_offset,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "ExtractionWorker: batch raised channel=%s rows=%d sync_job_id=%s",
                channel_id,
                len(valid_keys),
                sync_job_id,
            )
            await self._finalize_failed(
                stores, keys_with_attempts, error=f"{type(exc).__name__}: {exc}"
            )
            return 0, len(valid_keys)
        finally:
            # Drain any sync_summary metric bucket that process_messages may
            # have left behind on the exception path. Idempotent: returns
            # {} when the success path already popped the bucket.
            try:
                from beever_atlas.services.batch_processor import _drain_sync_metrics

                _drain_sync_metrics(channel_id, sync_job_id)
            except Exception:  # noqa: BLE001
                pass

        duration_ms = int((time.monotonic() - started) * 1000)
        # NOTE: post-tick aggregation of batches_completed +
        # batch_results was removed — BatchProcessor now writes per
        # batch directly to the user-facing channel row (see
        # ``batch_processor.py`` near ``increment_batches_completed``).
        # This gives the UI live MetricsBar updates instead of waiting
        # for the entire tick (10+ batches, 5-15 min) to finish.
        if result.errors:
            # Phase 1.1 / Task 2.1.3 — per-sub-batch attribution (decision
            # D1). Walk the BatchBreakdowns and partition ``valid_keys``
            # into the rows that belong to a succeeding sub-batch versus
            # those that belong to a failing sub-batch. Sub-batch
            # granularity is the unit at which the LLM call succeeds or
            # fails, so this is the finest split that doesn't require
            # per-row provenance tracking inside BatchProcessor.
            succeeded_keys: list[tuple[str, str, str]] = []
            failed_keys: list[tuple[str, str, str]] = []
            bd_errors: list[str] = []
            breakdowns = list(getattr(result, "batch_breakdowns", None) or [])
            for bd in breakdowns:
                bd_keys = list(getattr(bd, "keys", None) or [])
                if getattr(bd, "error", None) is None:
                    succeeded_keys.extend(bd_keys)
                else:
                    failed_keys.extend(bd_keys)
                    bd_errors.append(str(getattr(bd, "error", "unknown"))[:100])

            # Edge case: no breakdown carried any keys (older format or a
            # deep error path). Fall through to the legacy all-or-nothing
            # behavior so we don't drop rows into limbo. The stale-recovery
            # sweep is the safety net beyond that.
            if not succeeded_keys and not failed_keys:
                err_summary = "; ".join(
                    str(e.get("error") or "unknown")[:100] for e in result.errors[:3]
                )
                await self._finalize_failed(
                    stores,
                    keys_with_attempts,
                    error=f"batch_errors={len(result.errors)}: {err_summary}",
                )
                failed_count = len(valid_keys)
                logger.warning(
                    "ExtractionWorker: extraction_batch_complete "
                    "channel=%s rows=%d duration_ms=%d status=failed errors=%d "
                    "(legacy all-or-nothing path — breakdowns missing keys)",
                    channel_id,
                    failed_count,
                    duration_ms,
                    len(result.errors),
                )
                return 0, failed_count

            if succeeded_keys:
                await stores.mongodb.finalize_extraction_status_bulk(
                    keys=succeeded_keys, new_status="done"
                )
            if failed_keys:
                attempt_count_of = {k: a for k, a in keys_with_attempts}
                fail_pairs = [(k, attempt_count_of.get(k, 0)) for k in failed_keys]
                err_summary = "; ".join(bd_errors[:3]) if bd_errors else "unknown"
                await self._finalize_failed(
                    stores,
                    fail_pairs,
                    error=f"sub_batch_errors={len(failed_keys)}: {err_summary}",
                )

            # Notify subscribers with whatever fact_ids the succeeding
            # sub-batches produced — partial success still has signal.
            if succeeded_keys:
                fact_ids: list[str] = list(getattr(result, "fact_ids", None) or [])
                # memory-then-wiki-pipeline-realignment — accumulator path
                # fires per batch. ``_emit_extraction_done`` ALSO fans out
                # to memory_changed subscribers during the deprecation
                # window; emitting both keeps legacy callers + new callers
                # working without double-counting on memory_changed
                # subscribers (they only fire once via _emit_extraction_done).
                await self._emit_extraction_done(channel_id, fact_ids)

            logger.warning(
                "ExtractionWorker: extraction_batch_complete channel=%s rows=%d "
                "succeeded=%d failed=%d duration_ms=%d sub_batch_errors=%d",
                channel_id,
                len(valid_keys),
                len(succeeded_keys),
                len(failed_keys),
                duration_ms,
                len(result.errors),
            )
            return len(succeeded_keys), len(failed_keys)

        # Success path: bulk-mark done, then notify subscribers.
        modified = await stores.mongodb.finalize_extraction_status_bulk(
            keys=valid_keys, new_status="done"
        )
        # ``result.fact_ids`` may not be populated by every BatchResult;
        # subscribers tolerate empty lists (they will route by channel_id
        # alone or run a deterministic re-scan).
        fact_ids: list[str] = list(getattr(result, "fact_ids", None) or [])
        await self._emit_extraction_done(channel_id, fact_ids)
        logger.info(
            "ExtractionWorker: extraction_batch_complete "
            "channel=%s rows=%d duration_ms=%d status=done modified=%d",
            channel_id,
            len(valid_keys),
            duration_ms,
            modified,
        )
        return modified, 0

    @staticmethod
    async def _finalize_failed(
        stores: Any,
        keys_with_attempts: list[tuple[tuple[str, str, str], int]],
        error: str,
    ) -> None:
        """Transition each failed row with its own backoff schedule."""
        now = datetime.now(tz=UTC)
        # Group by next_attempt_at offset so we can collapse same-offset
        # rows into one bulk_write call.
        by_offset: dict[int, list[tuple[str, str, str]]] = {}
        for key, attempt_count in keys_with_attempts:
            offset = _retry_backoff_seconds(attempt_count + 1)
            by_offset.setdefault(offset, []).append(key)
        for offset, keys in by_offset.items():
            await stores.mongodb.finalize_extraction_status_bulk(
                keys=keys,
                new_status="failed",
                last_error=error[:500],
                next_attempt_at=now + timedelta(seconds=offset),
            )


# ----------------------------------------------------------------------
# Module-level singleton (registered by SyncScheduler at startup)
# ----------------------------------------------------------------------

_worker_instance: ExtractionWorker | None = None


def init_extraction_worker(worker: ExtractionWorker) -> None:
    """Register the process-wide ExtractionWorker instance.

    Used by :class:`SyncScheduler` at startup so other services
    (WikiMaintainer, admin endpoints) can subscribe to events without
    threading a reference through every constructor.
    """
    global _worker_instance
    _worker_instance = worker


def get_extraction_worker() -> ExtractionWorker | None:
    """Return the registered ExtractionWorker, or None before startup."""
    return _worker_instance
