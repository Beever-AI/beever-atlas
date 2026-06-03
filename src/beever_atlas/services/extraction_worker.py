"""Background extraction worker.

Drains pending messages from the durable Message Store and feeds them
through the existing ``BatchProcessor`` so sync (fetch + persist) is
decoupled from extraction (LLM calls). Sync writes ``ChannelMessage``
rows with ``extraction_status="pending"`` and returns; this worker —
registered as periodic APScheduler jobs by :class:`SyncScheduler` —
claims them atomically via ``find_one_and_update``, runs extraction,
and bulk-updates status to ``done`` or ``failed``.

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

if TYPE_CHECKING:
    from beever_atlas.services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


MemoryEventCallback = Callable[[str, list[str]], Awaitable[None] | None]
"""Signature for ``memory_changed`` / ``memory_settled`` subscribers
(channel_id, fact_ids).

Two events replace the prior single ``on_extraction_done`` contract:

- ``memory_changed`` fires after each successful batch. Subscribers that
  react to AT LEAST ONE memory change (e.g. incremental wiki page touches)
  use this. ``fact_ids`` is the freshly-extracted batch.
- ``memory_settled`` fires once a channel has no more pending rows for
  this tick — the channel's memory is at rest. Subscribers that need a
  stable channel state (debounced wiki flush, consolidation cycle,
  auto-overview) use this. ``fact_ids`` is the empty list (subscribers
  read accumulated state from stores).

The worker fans out via ``asyncio.gather`` and tolerates per-callback
failures so one buggy subscriber cannot block extraction progress.
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


_RETRY_BACKOFF_SCHEDULE: list[int] = [30, 60, 120, 240, 480]
"""Exponential backoff per attempt (seconds). Capped at the tail
for attempts beyond the schedule. Combined with ``_MAX_RETRIES``,
gives the system ~17 minutes of soft retries before a row stays
``failed`` permanently."""

_MAX_RETRIES: int = len(_RETRY_BACKOFF_SCHEDULE)
"""Max retry attempts before a failed row stays failed permanently.
Tied to the backoff schedule length so the two cannot drift."""

_TICK_SECONDS: int = 30
"""Default scheduler tick interval. Operators don't tune this — if
30s is wrong for some deployment, change it here and redeploy."""

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
        settle_seconds: int = 5,
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
        self._on_memory_changed: list[MemoryEventCallback] = []
        self._on_memory_settled: list[MemoryEventCallback] = []
        # Rolling-window metrics for the admin observability endpoint
        # (production-wiring §20). Each entry is a per-tick record:
        # ``(monotonic_ts, claimed, succeeded, failed)``. Trimmed to the
        # most recent ~10 minutes worth of ticks (well past the 60-min
        # window we summarise — a tick is at most every ``_TICK_SECONDS``,
        # so 10 min ≈ 20 entries).
        self._tick_records: list[tuple[float, int, int, int]] = []
        # Most recent failed-row records (capped at 10) for the admin
        # endpoint's ``recent_failures`` field. Each entry is
        # ``{message_id, channel_id, error_class, ts}``.
        self._recent_failures: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Event subscription (memory_changed / memory_settled)
    # ------------------------------------------------------------------

    def subscribe_memory_changed(self, callback: MemoryEventCallback) -> None:
        """Register a coroutine called after each successful extraction batch.

        Subscribers receive ``(channel_id, fact_ids)`` for the batch that
        just landed. Synchronous callbacks are tolerated. :class:`WikiMaintainer`
        uses this to mark affected wiki pages dirty so they refresh
        incrementally instead of waiting on full consolidation.
        """
        self._on_memory_changed.append(callback)

    def subscribe_memory_settled(self, callback: MemoryEventCallback) -> None:
        """Register a coroutine called when a channel's memory settles.

        "Settled" means a tick observed zero pending/extracting rows for the
        channel after draining its claimed batches. Subscribers receive
        ``(channel_id, [])``; they read accumulated state from the stores
        rather than the (empty) fact_ids list. Use this for debounced wiki
        flushes, consolidation cycles, and auto-overview rebuilds — work
        that should fire once per "stable channel state", not per batch.
        """
        self._on_memory_settled.append(callback)

    async def _emit_memory_changed(self, channel_id: str, fact_ids: list[str]) -> None:
        for cb in self._on_memory_changed:
            try:
                result = cb(channel_id, fact_ids)
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 — one bad subscriber must not stall extraction
                logger.exception(
                    "ExtractionWorker: memory_changed subscriber raised "
                    "for channel=%s fact_count=%d (continuing)",
                    channel_id,
                    len(fact_ids),
                )

    async def _emit_memory_settled(self, channel_id: str) -> None:
        for cb in self._on_memory_settled:
            try:
                result = cb(channel_id, [])
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 — one bad subscriber must not stall extraction
                logger.exception(
                    "ExtractionWorker: memory_settled subscriber raised "
                    "for channel=%s (continuing)",
                    channel_id,
                )

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
        claimed = await stores.mongodb.claim_pending_messages_for_extraction(
            batch_size=claim_size,
            channel_id=channel_id,
            settle_seconds=self._settle_seconds,
            max_retries=_MAX_RETRIES,
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
        logger.info(
            "ExtractionWorker: tick complete claimed=%d succeeded=%d failed=%d channels=%d",
            counters["claimed"],
            counters["succeeded"],
            counters["failed"],
            counters["channels"],
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

        started = time.monotonic()
        try:
            # ``sync_job_id=None`` lets BatchProcessor resolve the user-facing
            # ``kind="sync"`` row for this channel and write progress straight
            # to it. No synthetic worker:* IDs, no bridge methods.
            result = await self._batch_processor.process_messages(
                messages=normalized,
                channel_id=channel_id,
                channel_name=channel_name,
                sync_job_id=None,
                ingestion_config=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "ExtractionWorker: batch raised channel=%s rows=%d",
                channel_id,
                len(valid_keys),
            )
            await self._finalize_failed(
                stores, keys_with_attempts, error=f"{type(exc).__name__}: {exc}"
            )
            return 0, len(valid_keys)

        duration_ms = int((time.monotonic() - started) * 1000)
        if result.errors:
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
                "channel=%s rows=%d duration_ms=%d status=failed errors=%d",
                channel_id,
                failed_count,
                duration_ms,
                len(result.errors),
            )
            return 0, failed_count

        # Success path: bulk-mark done, then notify subscribers.
        modified = await stores.mongodb.finalize_extraction_status_bulk(
            keys=valid_keys, new_status="done"
        )
        # ``result.fact_ids`` may not be populated by every BatchResult;
        # subscribers tolerate empty lists (they will route by channel_id
        # alone or run a deterministic re-scan).
        fact_ids: list[str] = list(getattr(result, "fact_ids", None) or [])
        await self._emit_memory_changed(channel_id, fact_ids)

        # Settle check: if no rows are still pending or actively extracting
        # for this channel, the channel's memory is at rest. Emit
        # ``memory_settled`` so debounced subscribers (wiki flush,
        # consolidation, auto-overview) can fire exactly once per stable
        # state. Best-effort — a count failure must not stall the success
        # path, so we log + skip the emit and let the next tick try again.
        try:
            status_counts = await stores.mongodb.count_channel_messages_by_status(channel_id)
            pending_left = int(status_counts.get("pending", 0)) + int(
                status_counts.get("extracting", 0)
            )
            if pending_left == 0:
                await self._emit_memory_settled(channel_id)
        except Exception:  # noqa: BLE001 — settle check is best-effort
            logger.exception(
                "ExtractionWorker: settle check failed channel=%s — "
                "memory_settled will fire on a subsequent tick",
                channel_id,
            )

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
