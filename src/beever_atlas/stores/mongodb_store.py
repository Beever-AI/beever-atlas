"""MongoDB store client using motor async driver."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import ValidationError

from beever_atlas.models import (
    ActivityEvent,
    ChannelSyncState,
    SyncJob,
    WriteIntent,
)
from beever_atlas.models.persistence import (
    EXTRACTION_STATUS_TRANSITIONS,
    ChannelMessage,
    ExternalSource,
    IdempotencyKeyRecord,
)
from beever_atlas.models.sync_policy import (
    ChannelPolicy,
    GlobalPolicyDefaults,
)

logger = logging.getLogger(__name__)


# delete-channel-v2 Wave 0 — default staleness threshold for the channel
# hard-purge lock. A ``channel_purge_locks`` doc whose ``started_at`` is
# older than this is treated as abandoned (a crashed purge): writers ignore
# it and the reaper re-invokes the purge. 15 minutes is a deliberate ceiling
# above the max expected purge duration (multi-store fan-out is seconds, not
# minutes) so a slow-but-succeeding purge is never double-run by the reaper.
# Should-fix #5 of the plan. Callers may override per-call.
PURGE_LOCK_STALE_AFTER_S: float = 900.0


class MongoDBStore:
    """Manages MongoDB collections for Beever Atlas."""

    def __init__(self, uri: str, db_name: str = "beever_atlas") -> None:
        self._client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
        self._db = self._client[db_name]
        self._sync_jobs = self._db["sync_jobs"]
        self._channel_sync_state = self._db["channel_sync_state"]
        self._write_intents = self._db["write_intents"]
        self._activity_events = self._db["activity_events"]
        self._channel_policies = self._db["channel_policies"]
        self._global_policy_defaults = self._db["global_policy_defaults"]
        self._pipeline_checkpoints = self._db["pipeline_checkpoints"]
        # Channel hard-purge lock (delete-channel-v2 Wave 0). A durable,
        # atomically-claimed gate (its OWN collection, decoupled from
        # ``channel_sync_state`` so data-purge never touches the gate). One
        # doc per in-progress purge: ``{channel_id (unique), state:"purging",
        # started_at, owner_principal_id}``. Writers check it; the reaper
        # re-runs stale locks. See ``claim_purge`` / ``release_purge``.
        self._channel_purge_locks = self._db["channel_purge_locks"]
        # Channel purge audit log (delete-channel-v2 Wave 2). A RETAINED
        # collection — it is the durable record of every hard-purge and is
        # NEVER referenced by ``purge_channel`` (which only deletes channel
        # *data*). One doc per purge run: ``{channel_id, principal_id,
        # purge_run_id, counts, errors, unlinked_from, ts}``. The
        # ``purge_run_id`` (UUID per invocation) keeps reaper re-runs of the
        # same channel distinguishable (should-fix #6).
        self._channel_audit_log = self._db["channel_audit_log"]
        # Durable Message Store: replaces the prior in-memory
        # ``list[NormalizedMessage]`` flow during sync and serves as the queue
        # substrate for the background ExtractionWorker.
        self._channel_messages = self._db["channel_messages"]
        # Push-source registry + idempotency replay cache.
        self._external_sources = self._db["external_sources"]
        self._idempotency_keys = self._db["idempotency_keys"]
        # Wiki page-voice drift A/B reports — TTL=30d, populated by
        # ``services.wiki_drift_comparator`` when ``WIKI_DRIFT_AB=true``.
        # Aggregated by the ``GET /api/admin/wiki-drift/summary`` endpoint
        # to drive the soak-pass dashboard.
        self._wiki_drift_reports = self._db["wiki_drift_reports"]
        # ``wiki-llm-native-redesign`` collections.
        # ``wiki_merge_proposals`` carries operator-actionable suggestions
        # surfaced by ``WikiMaintainer._record_merge_proposals`` whenever
        # two pages cross the ``WIKI_PAGE_MERGE_THRESHOLD`` Jaccard bar.
        self._wiki_merge_proposals = self._db["wiki_merge_proposals"]
        # ``wiki_proposed_edits`` is reserved for §7.9 (v2 agent
        # write-through ``propose_wiki_edit``). v1 does NOT write here;
        # the collection is created with TTL/indexes so the v2 ship is
        # a code-only change.
        self._wiki_proposed_edits = self._db["wiki_proposed_edits"]
        # memory-then-wiki-pipeline-realignment — durable per-page dirty
        # queue. Replaces the in-memory ``WikiMaintainer._dirty`` dict so
        # backend crashes during the debounce window no longer lose
        # queued page rewrites. See specs/wiki-dirty-queue/spec.md.
        self._wiki_dirty_queue = self._db["wiki_dirty_queue"]
        # P0-2: media extractor content-hash cache.  One document per unique
        # (hash, mime_type) pair; compound unique index created in startup().
        from beever_atlas.stores.media_cache_store import MediaCacheStore

        self._media_cache_store = MediaCacheStore(self._db["media_cache"])

    @property
    def db(self):
        """Expose the underlying AsyncIOMotorDatabase for stores that need it."""
        return self._db

    @property
    def wiki_merge_proposals(self):
        """Operator-facing merge suggestions surfaced by the maintainer
        when two pages share enough facts to suggest consolidation."""
        return self._wiki_merge_proposals

    @property
    def wiki_proposed_edits(self):
        """Reserved for §7.9 — v2 agent ``propose_wiki_edit`` writes here."""
        return self._wiki_proposed_edits

    @property
    def media_cache(self):
        """P0-2: content-hash cache for media extractor outputs."""
        return self._media_cache_store

    async def startup(self) -> None:
        """Ping MongoDB to verify the connection is alive."""
        await self._client.admin.command("ping")
        await self._sync_jobs.create_index([("channel_id", 1), ("started_at", -1)])
        await self._write_intents.create_index(
            [("created_at", 1), ("weaviate_done", 1), ("neo4j_done", 1)]
        )
        await self._channel_sync_state.create_index("channel_id", unique=True)
        await self._activity_events.create_index([("timestamp", -1)])
        await self._channel_policies.create_index("channel_id", unique=True)
        await self._pipeline_checkpoints.create_index("batch_key", unique=True)
        # delete-channel-v2 Wave 0 — channel hard-purge lock + channel-scoped
        # purge indexes.
        #
        # Index-on-missing-field decision: ``write_intents.channel_id`` and
        # ``pipeline_checkpoints.channel_id`` are written in Wave 1, but
        # ``create_index`` does NOT error when the field is absent on existing
        # documents — Mongo simply indexes those rows under a null key (a
        # standard non-sparse, non-unique index). Creating the indexes now is
        # therefore idempotent and forward-safe; Wave 1 only has to start
        # *writing* the field. (Verified: a plain ascending index over a
        # not-yet-present field is a no-op create that any later backfill /
        # write picks up incrementally.)
        await self._channel_purge_locks.create_index("channel_id", unique=True)
        await self._write_intents.create_index("channel_id", name="write_intents_channel_id")
        await self._pipeline_checkpoints.create_index(
            "channel_id", name="pipeline_checkpoints_channel_id"
        )
        # memory-then-wiki-pipeline-realignment — wiki_dirty_queue indexes.
        # Primary lookup is by ``(channel_id, page_id)`` for upsert; status
        # field is the predicate for claim/recover sweeps.
        await self._wiki_dirty_queue.create_index(
            [("channel_id", 1), ("page_id", 1)],
            unique=True,
            name="wiki_dirty_queue_channel_page_unique",
        )
        # Stale-flushing recovery: scan rows by status + age.
        await self._wiki_dirty_queue.create_index(
            [("status", 1), ("updated_at", 1)],
            name="wiki_dirty_queue_status_age",
        )
        # ``channel_messages`` indexes.
        # 1) Compound unique key for idempotent upsert.
        await self._channel_messages.create_index(
            [("source_id", 1), ("channel_id", 1), ("message_id", 1)],
            unique=True,
            name="channel_messages_source_channel_message_unique",
        )
        # 2) Secondary index for UI list reads (timestamp DESC for newest-first).
        await self._channel_messages.create_index(
            [("channel_id", 1), ("timestamp", -1)],
            name="channel_messages_channel_timestamp",
        )
        # 3) Sparse index for the ExtractionWorker queue scan.
        # ``extraction_status`` is always set on insert, but the sparse condition
        # keeps the index cheap by excluding ``done`` rows from the workload.
        await self._channel_messages.create_index(
            [("extraction_status", 1), ("next_attempt_at", 1)],
            name="channel_messages_status_next_attempt",
            partialFilterExpression={
                "extraction_status": {"$in": ["pending", "extracting", "failed"]}
            },
        )
        # Push-source registry + idempotency replay cache indexes.
        await self._external_sources.create_index(
            "source_id",
            unique=True,
            name="external_sources_source_id_unique",
        )
        await self._idempotency_keys.create_index(
            [("source_id", 1), ("idempotency_key", 1)],
            unique=True,
            name="idempotency_keys_compound_unique",
        )
        # 24h TTL — Mongo deletes documents whose ``created_at`` is older.
        await self._idempotency_keys.create_index(
            "created_at",
            expireAfterSeconds=86400,
            name="idempotency_keys_ttl",
        )
        # ``wiki_drift_reports`` indexes:
        # 1) TTL — documents auto-expire 30 days after their inserted ``ts``.
        await self._wiki_drift_reports.create_index(
            [("ts", 1)],
            expireAfterSeconds=2592000,
            name="wiki_drift_reports_ttl",
        )
        # 2) Compound (channel_id, ts DESC) for the per-channel + recent
        # query the summary endpoint runs.
        await self._wiki_drift_reports.create_index(
            [("channel_id", 1), ("ts", -1)],
            name="wiki_drift_reports_channel_ts",
        )
        # ``wiki_merge_proposals`` indexes:
        # 1) Compound (channel_id, status, surfaced_at DESC) so the
        # operator UI's "open proposals" query is one scan over a
        # bounded range. Status values: 'open' | 'approved' | 'rejected'.
        await self._wiki_merge_proposals.create_index(
            [("channel_id", 1), ("status", 1), ("surfaced_at", -1)],
            name="wiki_merge_proposals_channel_status_surfaced",
        )
        # 2) Idempotency on (channel_id, target_lang, source_slug, target_slug)
        # so re-firing on_extraction_done with stable inputs does not
        # double-record the same suggestion.
        await self._wiki_merge_proposals.create_index(
            [
                ("channel_id", 1),
                ("target_lang", 1),
                ("source_slug", 1),
                ("target_slug", 1),
            ],
            unique=True,
            name="wiki_merge_proposals_compound_unique",
        )
        # ``wiki_proposed_edits`` (§7.8 — reserved for v2):
        # 1) TTL — proposals expire after 30 days so a stale list cannot
        # accumulate unbounded.
        await self._wiki_proposed_edits.create_index(
            [("created_at", 1)],
            expireAfterSeconds=30 * 24 * 3600,
            name="wiki_proposed_edits_ttl",
        )
        # 2) Compound (channel_id, slug, status) for the operator UI
        # query "open proposals on this page".
        await self._wiki_proposed_edits.create_index(
            [("channel_id", 1), ("slug", 1), ("status", 1)],
            name="wiki_proposed_edits_channel_slug_status",
        )
        # delete-channel-v2 Wave 2 — retained channel purge audit log.
        # Compound (channel_id, ts DESC) so an operator can pull a single
        # channel's purge history (incl. reaper re-runs distinguished by
        # ``purge_run_id``) in one scan, newest-first. NOT a TTL index —
        # this collection is the durable record and must outlive the data.
        await self._channel_audit_log.create_index(
            [("channel_id", 1), ("ts", -1)],
            name="channel_audit_log_channel_ts",
        )
        # P0-2: media extractor content-hash cache index.
        await self._media_cache_store.ensure_indexes()
        # Seed global policy defaults from Settings if not present
        existing = await self._global_policy_defaults.find_one({"id": "global"})
        if existing is None:
            from beever_atlas.infra.config import get_settings
            from beever_atlas.models.sync_policy import (
                ConsolidationConfig,
                ConsolidationStrategy,
                IngestionConfig,
                SyncConfig,
                SyncTriggerMode,
            )

            s = get_settings()
            defaults = GlobalPolicyDefaults(
                sync=SyncConfig(
                    trigger_mode=SyncTriggerMode.MANUAL,
                    sync_type="auto",
                    max_messages=s.sync_max_messages,
                    min_sync_interval_minutes=1,
                ),
                ingestion=IngestionConfig(
                    batch_size=s.sync_batch_size,
                    quality_threshold=s.quality_threshold,
                    max_facts_per_message=s.max_facts_per_message,
                    skip_entity_extraction=False,
                    skip_graph_writes=False,
                ),
                consolidation=ConsolidationConfig(
                    strategy=ConsolidationStrategy.AFTER_EVERY_SYNC,
                    after_n_syncs=3,
                    similarity_threshold=s.cluster_similarity_threshold,
                    merge_threshold=s.cluster_merge_threshold,
                    min_facts_for_clustering=3,
                    staleness_refresh_days=7,
                ),
            )
            await self._global_policy_defaults.insert_one(defaults.model_dump(mode="json"))

    async def shutdown(self) -> None:
        """Close the MongoDB client connection."""
        self._client.close()

    # ------------------------------------------------------------------
    # Sync jobs
    # ------------------------------------------------------------------

    async def create_sync_job(
        self,
        channel_id: str,
        sync_type: str,
        total_messages: int,
        batch_size: int = 10,
        parent_messages: int = 0,
        owner_principal_id: str | None = None,
        kind: str = "sync",
    ) -> SyncJob:
        """Create and persist a new SyncJob, returning the model.

        ``owner_principal_id`` is stamped on new rows so MCP's
        ``get_job_status`` can enforce ``job_not_found`` for jobs the
        caller does not own. Pre-migration rows lack this field; readers
        MUST treat missing/None values as owned by the ``"legacy:shared"``
        sentinel.
        """
        # Defensive: any prior ``running`` row for this channel/kind is
        # orphaned by construction — a new sync was just triggered.
        # Mark such rows as ``orphaned`` and clear their ``batch_results`` +
        # ``batches_completed`` so the brief window between this insert
        # and the runner stamping the new row can't leak the previous
        # run's done chips to ``/sync/status``. ``orphaned`` is treated as
        # a terminal status by ``get_sync_status``'s status filter — it
        # only prefers ``running`` rows, so an ``orphaned`` row is invisible
        # to that path.
        await self._sync_jobs.update_many(
            {
                "channel_id": channel_id,
                "kind": kind,
                "status": "running",
            },
            {
                "$set": {
                    "status": "orphaned",
                    "batch_results": [],
                    "batches_completed": 0,
                }
            },
        )
        job = SyncJob(
            channel_id=channel_id,
            sync_type=sync_type,
            total_messages=total_messages,
            parent_messages=parent_messages or total_messages,
            batch_size=batch_size,
            owner_principal_id=owner_principal_id,
            kind=kind,
        )
        await self._sync_jobs.insert_one(job.model_dump())
        return job

    async def update_sync_progress(
        self,
        job_id: str,
        processed: int,
        current_batch: int,
        current_stage: str | None = None,
        stage_timings: dict[str, float] | None = None,
        stage_details: dict[str, Any] | None = None,
        total_batches: int | None = None,
        batch_result: dict[str, Any] | None = None,
    ) -> None:
        """Update processed message count, current batch index, and optional stage."""
        update: dict[str, Any] = {
            "processed_messages": processed,
            "current_batch": current_batch,
        }
        if total_batches is not None:
            update["total_batches"] = total_batches
        if current_stage is not None:
            update["current_stage"] = current_stage
        if stage_timings is not None:
            update["stage_timings"] = stage_timings
        if stage_details is not None:
            update["stage_details"] = stage_details

        ops: dict[str, Any] = {"$set": update, "$inc": {"version": 1}}
        if batch_result is not None:
            ops["$push"] = {"batch_results": batch_result}

        await self._sync_jobs.update_one({"id": job_id}, ops)

    async def refresh_sync_progress_for_channel(self, channel_id: str) -> None:
        """Patch the latest ``sync_jobs`` row's progress fields from the
        ``channel_messages.extraction_status`` source of truth.

        The decoupled-mode ``ExtractionWorker`` doesn't have a direct
        update path to ``sync_jobs`` — it synthesises a per-tick
        ``worker:<channel>:<ts>`` id and feeds the BatchProcessor with
        that, so ``BatchProcessor.update_sync_progress`` writes go to
        a synthetic id that nobody reads. The user-facing sync_jobs
        row therefore stays at ``processed_messages=0`` /
        ``current_stage=""`` for the entire run, even when extraction is
        actively producing facts.

        This helper is the bridge: it aggregates counts from the
        durable ``channel_messages`` collection (the actual source of
        truth) and patches the most recent sync row for the channel.
        Idempotent — safe to call after every worker batch.
        """
        pipeline = [
            {"$match": {"channel_id": channel_id}},
            {"$group": {"_id": "$extraction_status", "count": {"$sum": 1}}},
        ]
        counts: dict[str, int] = {}
        async for doc in self._channel_messages.aggregate(pipeline):
            status = doc.get("_id") or "unknown"
            counts[str(status)] = int(doc.get("count") or 0)

        done = counts.get("done", 0)
        extracting = counts.get("extracting", 0)
        pending = counts.get("pending", 0)
        failed = counts.get("failed", 0)

        if extracting > 0:
            stage = f"Extracting — {extracting} in flight"
        elif pending > 0:
            stage = f"Queued — {pending} pending"
        elif failed > 0 and done == 0:
            stage = f"Extraction failed — {failed} rows"
        else:
            stage = "Extraction complete"

        # ``sort=[("created_at", -1)]`` picks the most-recent sync row
        # so re-triggering a sync naturally retargets the new row.
        # ``find_one_and_update`` (not ``update_one``) is the only motor
        # call that supports sort — needed because we have no other way
        # to disambiguate when multiple sync rows exist per channel.
        await self._sync_jobs.find_one_and_update(
            {"channel_id": channel_id, "kind": "sync"},
            {
                "$set": {
                    "processed_messages": done,
                    "current_stage": stage,
                },
                "$inc": {"version": 1},
            },
            sort=[("created_at", -1)],
        )

    async def set_sync_job_totals(
        self,
        job_id: str,
        total_messages: int,
        parent_messages: int,
        sync_type: str | None = None,
        total_batches: int | None = None,
    ) -> None:
        """Patch a sync job's message totals after creation.

        Used by the async sync-trigger path: the API endpoint creates a
        placeholder job row (total_messages=0) and returns immediately
        with the job_id, then a background task does the slow bridge
        fetch and calls this method to fill in the real totals before
        the pipeline starts processing.

        ``sync_type`` is optional because the type may be promoted from
        ``incremental`` to ``full`` mid-fetch (when an incremental sync
        finds zero new messages and falls back to a full re-pull).

        ``total_batches`` is the global batch count for the whole sync
        (``ceil(total_messages / batch_size)``) — stable for the entire
        run. Without this, the user-facing sync_jobs row's total_batches
        stays 0 because the decoupled ExtractionWorker only writes
        per-tick totals to synthetic ``worker:*`` rows.
        """
        update: dict[str, Any] = {
            "total_messages": total_messages,
            "parent_messages": parent_messages,
        }
        if sync_type is not None:
            update["sync_type"] = sync_type
        if total_batches is not None:
            update["total_batches"] = total_batches
        await self._sync_jobs.update_one(
            {"id": job_id},
            {"$set": update, "$inc": {"version": 1}},
        )

    async def update_batch_stage(
        self,
        job_id: str,
        batch_idx: int,
        label: str,
    ) -> None:
        """Atomic dot-path update for per-batch stage label — race-safe under concurrency.

        Writes stage_details.batch_stages.<batch_idx> without touching sibling
        batch entries. Also keeps the deprecated singleton current_stage / current_batch
        fields so worker-4 (frontend) can fall back when batch_stages is absent.
        """
        await self._sync_jobs.update_one(
            {"id": job_id},
            {
                "$set": {
                    f"stage_details.batch_stages.{batch_idx}": label,
                    # deprecated singletons — kept for backward compat with frontend fallback
                    "current_stage": label,
                    "current_batch": batch_idx,
                },
                "$inc": {"version": 1},
            },
        )

    async def push_activity_log_entry(
        self,
        job_id: str,
        batch_idx: int,
        entry: dict[str, Any],
    ) -> None:
        """Append a batch-tagged entry to the activity log, capped at 50.

        Tags the entry with batch_idx so the frontend can group/filter per batch.
        Uses $push + $slice to avoid unbounded growth — race-safe under concurrency.

        memory-then-wiki-pipeline-realignment fix: ``upsert=True`` so the
        synthetic ``worker:<channel>:<ts>`` job_ids used by the decoupled
        ExtractionWorker auto-create a sync_jobs document on first write.
        Without this, activity_log entries silently dropped because the
        target document never existed. Parses channel_id + started_at
        from the synthetic id format so the merge in
        ``list_recent_activity_log`` (used by /sync/status) can find it.
        """
        tagged_entry = {**entry, "batch_idx": batch_idx}
        # Parse channel_id + started_at from the synthetic worker job_id
        # so the upsert document carries enough metadata for the merge
        # query to find it. Format: ``worker:<channel_id>:<epoch_ms>``.
        set_on_insert: dict[str, Any] = {}
        if job_id.startswith("worker:"):
            try:
                parts = job_id.split(":", 2)
                if len(parts) == 3:
                    set_on_insert["channel_id"] = parts[1]
                    # epoch_ms → ISO datetime so list_recent_activity_log
                    # can filter by ``started_at >= main_job.started_at``.
                    epoch_ms = int(parts[2])
                    set_on_insert["started_at"] = datetime.fromtimestamp(
                        epoch_ms / 1000.0, tz=UTC
                    ).isoformat()
                    set_on_insert["kind"] = "worker_extraction"
                    set_on_insert["status"] = "running"
            except Exception:  # noqa: BLE001 — best-effort
                pass
        update: dict[str, Any] = {
            "$push": {
                "stage_details.activity_log": {
                    "$each": [tagged_entry],
                    "$slice": -500,
                }
            },
            "$inc": {"version": 1},
        }
        if set_on_insert:
            update["$setOnInsert"] = set_on_insert
        await self._sync_jobs.update_one(
            {"id": job_id},
            update,
            upsert=True,
        )

    async def increment_batches_completed(self, job_id: str) -> None:
        """Atomic increment of batches_completed — safe under concurrent batch runs."""
        await self._sync_jobs.update_one(
            {"id": job_id},
            {"$inc": {"batches_completed": 1, "version": 1}},
        )

    async def increment_batches_completed_for_channel(
        self,
        channel_id: str,
        count: int,
        max_batch_num: int | None = None,
    ) -> None:
        """Increment ``batches_completed`` on the most-recent user-facing
        sync_jobs row for a channel.

        The decoupled ExtractionWorker uses synthetic ``worker:*`` job_ids
        so ``increment_batches_completed(job_id)`` calls inside BatchProcessor
        never reach the row the UI reads. This helper bridges that gap —
        the worker calls it once per tick with ``count = len(breakdowns)``,
        keeping the user-facing row's global ``batches_completed`` accurate
        for the BATCHES tile and as the offset for the next tick's global
        batch_index numbering.
        """
        if count <= 0:
            return
        # First bump batches_completed atomically.
        doc = await self._sync_jobs.find_one_and_update(
            {"channel_id": channel_id, "kind": "sync"},
            {"$inc": {"batches_completed": count, "version": 1}},
            sort=[("created_at", -1)],
            return_document=True,
        )
        # SyncRunner's initial ``total_batches`` is a fixed-size estimate
        # (``ceil(total_messages / sync_batch_size)``) but the worker
        # actually uses token-aware batching that can yield more, smaller
        # batches. Bump ``total_batches`` to track the high-water mark of
        # ``batches_completed`` so the API never returns "21 done / 15
        # total" nonsense.
        if doc is None:
            return
        completed = int(doc.get("batches_completed") or 0)
        total = int(doc.get("total_batches") or 0)
        # SyncRunner's initial ``total_batches`` is a fixed-size estimate
        # (``ceil(total_messages / sync_batch_size)``). Token-aware
        # batching in BatchProcessor can yield more batches than the
        # estimate, so bump ``total_batches`` to the high-water mark of
        # either the completed counter OR the actual batch_num the
        # caller is reporting. Without this, the UI shows weirdness
        # like ``14/15`` while the chip strip extends to Batch 21.
        target = max(completed, max_batch_num or 0)
        if target > total:
            await self._sync_jobs.update_one(
                {"id": doc.get("id")},
                {"$set": {"total_batches": target}, "$inc": {"version": 1}},
            )

    async def append_batch_results_for_channel(
        self,
        channel_id: str,
        batch_results: list[dict[str, Any]],
    ) -> None:
        """Append per-batch breakdown rows to the user-facing sync_jobs
        row for a channel.

        Without this bridge, BatchProcessor's ``update_sync_progress``
        calls only persist to the synthetic ``worker:*`` job_ids that
        nobody reads — and the frontend's MetricsBar tiles end up
        reflecting only whatever happens to still be in the
        ``activity_log`` $slice buffer. With this bridge, each tick's
        batch breakdowns land on the row the UI polls, so per-batch
        facts/entities/embedded/media counts are accurate even after
        the activity_log entries evict.
        """
        if not batch_results:
            return
        await self._sync_jobs.find_one_and_update(
            {"channel_id": channel_id, "kind": "sync"},
            {
                "$push": {"batch_results": {"$each": batch_results}},
                "$inc": {"version": 1},
            },
            sort=[("created_at", -1)],
        )

    async def get_user_facing_batches_completed(self, channel_id: str) -> int:
        """Return ``batches_completed`` from the most-recent user-facing
        sync_jobs row for a channel. Used by ExtractionWorker as the global
        ``batch_index_offset`` for BatchProcessor invocations.
        """
        doc = await self._sync_jobs.find_one(
            {"channel_id": channel_id, "kind": "sync"},
            {"_id": 0, "batches_completed": 1},
            sort=[("created_at", -1)],
        )
        if not doc:
            return 0
        return int(doc.get("batches_completed") or 0)

    async def complete_sync_job(
        self,
        job_id: str,
        status: str,
        errors: list[str] | None = None,
        failed_stage: str | None = None,
        failed_batches: list[dict[str, Any]] | None = None,
    ) -> None:
        """Mark a sync job as completed or failed with an optional error list.

        ``failed_batches`` records per-batch diagnostic context so an operator
        can identify which messages need manual recovery after a partial failure.
        """
        update: dict[str, Any] = {
            "status": status,
            "completed_at": datetime.now(tz=UTC),
        }
        if errors is not None:
            update["errors"] = errors
        if failed_stage is not None:
            update["current_stage"] = failed_stage
        if failed_batches is not None:
            update["failed_batches"] = failed_batches
        await self._sync_jobs.update_one({"id": job_id}, {"$set": update})

    async def get_sync_status(self, channel_id: str) -> SyncJob | None:
        """Return the active SyncJob for the given channel, or None.

        Prefers the currently-running sync row when one exists. Without
        this preference, the row returned by ``/sync/status`` during the
        brief window between trigger and the new ``sync_jobs`` row
        landing is the *previous* run's completed row — whose
        ``batch_results`` array then pollutes the frontend's chip strip
        with stale DONE batches before any new work has been done.

        Falls back to the most-recent ``kind="sync"`` row (any status) when
        no running sync exists so legacy callers reading historical state —
        e.g. cooldown checks in ``capabilities.sync`` — keep working.

        The synthetic ``worker_extraction`` rows (``worker:<channel>:<ts>``)
        are deliberately excluded from BOTH queries: the ExtractionWorker
        writes per-batch progress to them but never finalizes them to
        ``completed`` (they're internal telemetry — "a row nobody reads").
        Including them here meant a stuck ``running`` worker row was returned
        as the active job, so ``/sync/status`` reported a perpetual sync and
        the sidebar "Syncing now…" dot never cleared.
        """
        running = await self._sync_jobs.find_one(
            {"channel_id": channel_id, "kind": "sync", "status": "running"},
            sort=[("started_at", -1)],
        )
        if running is not None:
            running.pop("_id", None)
            return SyncJob(**running)
        latest = await self._sync_jobs.find_one(
            {"channel_id": channel_id, "kind": "sync"},
            sort=[("started_at", -1)],
        )
        if latest is None:
            return None
        latest.pop("_id", None)
        return SyncJob(**latest)

    async def list_recent_activity_log(
        self,
        channel_id: str,
        since_iso: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        """Merge activity_log entries from every recent sync_job for the channel.

        The decoupled ExtractionWorker writes its rich ``stage_output``
        entries to a synthetic ``worker:<channel>:<ts>`` sync_job_id —
        those entries don't land on the user-facing sync_job row that
        ``/sync/status`` returns. This method walks every sync_job for
        the channel started within the last ~10 minutes and concatenates
        their ``stage_details.activity_log`` arrays into one chronological
        feed for the SyncProgressV2 UI.

        Args:
            channel_id: Channel to fetch for.
            since_iso: ISO timestamp lower bound. When None, defaults to
                10 minutes ago so per-batch worker rows that finished long
                before the active sync are filtered out.
            limit: Cap on the merged result — newest-first within each
                job, then concatenated jobs newest-first.
        """
        if since_iso is None:
            since = datetime.now(tz=UTC) - timedelta(minutes=10)
            since_iso = since.isoformat()
        cursor = self._sync_jobs.find(
            {
                "channel_id": channel_id,
                "started_at": {"$gte": since_iso},
            },
            {"_id": 0, "id": 1, "started_at": 1, "stage_details.activity_log": 1},
        ).sort("started_at", -1)
        merged: list[dict[str, Any]] = []
        async for doc in cursor:
            entries = (doc.get("stage_details") or {}).get("activity_log") or []
            merged.extend(entries)
            if len(merged) >= limit:
                break
        return merged[:limit]

    async def get_last_job_by_kind(self, channel_id: str, kind: str) -> SyncJob | None:
        """Return the most recent SyncJob of ``kind`` for *channel_id*, or None.

        Used by the wiki cooldown check so a ``sync`` job does not count
        against the ``wiki_refresh`` window (Fix #4).
        """
        doc = await self._sync_jobs.find_one(
            {"channel_id": channel_id, "kind": kind},
            sort=[("started_at", -1)],
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return SyncJob(**doc)

    async def get_sync_job(self, job_id: str) -> SyncJob | None:
        """Return the ``SyncJob`` for the given id, or ``None`` if missing.

        Public accessor used by the MCP ``get_job_status`` capability; keeps
        the private ``_sync_jobs`` collection encapsulated so a schema
        refactor (rename, index change, partitioning) does not silently
        break cross-module callers.
        """
        doc = await self._sync_jobs.find_one({"id": job_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return SyncJob(**doc)

    async def get_sync_jobs_for_channel(
        self,
        channel_id: str,
        limit: int = 20,
    ) -> list[SyncJob]:
        """Return past sync jobs for a channel, newest first."""
        cursor = self._sync_jobs.find(
            {"channel_id": channel_id},
            sort=[("started_at", -1)],
            limit=limit,
        )
        jobs: list[SyncJob] = []
        async for doc in cursor:
            doc.pop("_id", None)
            jobs.append(SyncJob(**doc))
        return jobs

    async def save_fact_statuses(
        self,
        job_id: str,
        batch_num: int,
        statuses: list[dict[str, Any]],
    ) -> None:
        """Upsert fact status array into a batch result entry.

        Each status dict has: fact_index, status, weaviate_id, error, retry_count
        """
        # Use array filter to update the specific batch_result entry by batch_num
        # If no matching batch_result exists, push a new entry
        await self._sync_jobs.update_one(
            {"id": job_id, "batch_results.batch_num": batch_num},
            {"$set": {"batch_results.$.fact_statuses": statuses}},
        )

    async def get_failed_facts(
        self,
        job_id: str,
        max_retries: int = 3,
    ) -> list[dict[str, Any]]:
        """Return facts with status 'failed' and retry_count < max_retries."""
        job = await self._sync_jobs.find_one({"id": job_id})
        if not job:
            return []
        failed: list[dict[str, Any]] = []
        for batch_result in job.get("batch_results", []):
            for fact in batch_result.get("fact_statuses", []):
                if fact.get("status") == "failed" and fact.get("retry_count", 0) < max_retries:
                    failed.append(
                        {
                            "batch_num": batch_result.get("batch_num"),
                            **fact,
                        }
                    )
        return failed

    async def get_channel_sync_state(self, channel_id: str) -> ChannelSyncState | None:
        """Return the ChannelSyncState for the given channel, or None."""
        doc = await self._channel_sync_state.find_one({"channel_id": channel_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return ChannelSyncState(**doc)

    async def get_channel_sync_states_batch(
        self, channel_ids: list[str]
    ) -> dict[str, ChannelSyncState]:
        """Return a map of channel_id -> ChannelSyncState using a single $in query."""
        if not channel_ids:
            return {}
        result: dict[str, ChannelSyncState] = {}
        cursor = self._channel_sync_state.find({"channel_id": {"$in": list(channel_ids)}})
        async for doc in cursor:
            cid = doc.get("channel_id")
            doc.pop("_id", None)
            if cid:
                try:
                    result[cid] = ChannelSyncState(**doc)
                except ValidationError as exc:
                    # Issue #40 — log corrupt / schema-drifted documents so
                    # operators can diagnose silent channel disappearances.
                    # The single-record `get_channel_sync_state` raises on
                    # the same condition; we keep the batch path tolerant
                    # (skip + log) so one corrupt row doesn't poison the
                    # whole sync-status response.
                    logger.warning(
                        "get_channel_sync_states_batch: failed to deserialize channel %s: %s",
                        cid,
                        exc,
                    )
                    continue
        return result

    async def update_channel_sync_state(
        self,
        channel_id: str,
        last_sync_ts: str,
        increment: int = 0,
        set_total: int | None = None,
    ) -> None:
        """Upsert the channel sync state, optionally incrementing message count."""
        update: dict[str, Any] = {"$set": {"last_sync_ts": last_sync_ts}}
        if set_total is not None:
            update["$set"]["total_synced_messages"] = set_total
        if increment:
            update["$inc"] = {"total_synced_messages": increment}
        await self._channel_sync_state.update_one(
            {"channel_id": channel_id},
            update,
            upsert=True,
        )

    async def clear_channel_sync_state(self, channel_id: str) -> None:
        """Delete the sync state for a channel, forcing a full re-sync next time."""
        await self._channel_sync_state.delete_one({"channel_id": channel_id})
        await self._sync_jobs.delete_many({"channel_id": channel_id})

    async def purge_channel(self, channel_id: str) -> dict[str, int]:
        """Hard-delete every Mongo document this store owns for ``channel_id``.

        delete-channel-v2 Wave 1 aggregator. Deletes per-collection and
        returns a ``{collection_name: deleted_count}`` map so the Wave 2
        fan-out service can record honest per-store counts in the audit log.

        Field names were verified against each write path (NOT assumed):
          * ``channel_messages`` — top-level ``channel_id`` (ChannelMessage).
          * ``imported_messages`` — top-level ``channel_id`` (api/imports.py).
            Legacy collection mid-migration with NO dedicated store attr, so
            it is reached via ``self._db[...]`` and wrapped in try/except —
            its absence must not abort the purge.
          * ``activity_events`` — ``channel_id`` top-level, but sync-history
            rows also carry it under ``details.channel_id`` (ActivityEvent),
            so an ``$or`` covers both.
          * ``wiki_dirty_queue`` / ``wiki_drift_reports`` /
            ``wiki_merge_proposals`` / ``wiki_proposed_edits`` — top-level
            ``channel_id`` (verified via their indexes + write paths).
          * ``write_intents`` — top-level ``channel_id`` (Wave 1 field, now
            indexed). Mixed-channel / legacy-null intents are NOT matched
            here; the reconciler's per-fact filter neutralises them.
          * ``pipeline_checkpoints`` — top-level ``channel_id`` written by
            ``save_pipeline_checkpoint`` (no regex/join on ``batch_key``).

        Does NOT touch ``channel_purge_locks`` (the gate) or any audit log —
        those are managed by the Wave 2 service. ``clear_channel_sync_state``
        is reused at the end so ``channel_sync_state`` + ``sync_jobs`` are
        cleared with the same semantics the reset path already relies on.

        The counts for ``channel_sync_state`` and ``sync_jobs`` are reported
        separately (re-counted before the clear) because
        ``clear_channel_sync_state`` returns nothing.
        """
        # INVARIANT: never touch _channel_audit_log or _channel_purge_locks here.
        counts: dict[str, int] = {}

        async def _delete_many(coll: Any, query: dict[str, Any]) -> int:
            result = await coll.delete_many(query)
            return int(result.deleted_count or 0)

        counts["channel_messages"] = await _delete_many(
            self._channel_messages, {"channel_id": channel_id}
        )

        # Legacy collection mid-migration — no store attr; best-effort so a
        # missing collection / driver hiccup never aborts the rest of the purge.
        # A FAILURE here is real data loss (legacy messages survive the purge),
        # so we surface a sentinel (``imported_messages_error``) in the returned
        # counts. The Wave-2 service promotes that sentinel into ``errors`` so
        # the run is reported "partial" and the purge lock is RETAINED for the
        # reaper — without it a lone imported_messages failure would silently
        # release the lock and the reaper would never retry.
        try:
            counts["imported_messages"] = await _delete_many(
                self._db["imported_messages"], {"channel_id": channel_id}
            )
        except Exception:
            logger.warning(
                "purge_channel: imported_messages delete failed for channel=%s "
                "(best-effort, continuing)",
                channel_id,
                exc_info=True,
            )
            counts["imported_messages"] = 0
            counts["imported_messages_error"] = 1

        counts["activity_events"] = await _delete_many(
            self._activity_events,
            {"$or": [{"channel_id": channel_id}, {"details.channel_id": channel_id}]},
        )
        counts["wiki_dirty_queue"] = await _delete_many(
            self._wiki_dirty_queue, {"channel_id": channel_id}
        )
        counts["wiki_drift_reports"] = await _delete_many(
            self._wiki_drift_reports, {"channel_id": channel_id}
        )
        counts["wiki_merge_proposals"] = await _delete_many(
            self._wiki_merge_proposals, {"channel_id": channel_id}
        )
        counts["wiki_proposed_edits"] = await _delete_many(
            self._wiki_proposed_edits, {"channel_id": channel_id}
        )
        # ``wiki_versions`` — the published, versioned wiki snapshot that
        # ``GET /api/channels/{id}/wiki`` and the channel overview read from
        # (see api/sync.py). Purge-only: ``_reset_fanout`` deliberately
        # preserves the versioned wiki, so this lives here (the purge
        # aggregator), never on the reset path. Omitting it left the wiki served
        # (HTTP 200) after a hard delete even though ``wiki_pages`` was gone.
        # No dedicated store attr — access the collection via ``_db``.
        counts["wiki_versions"] = await _delete_many(
            self._db["wiki_versions"], {"channel_id": channel_id}
        )
        # ``wiki_version_counters`` — the atomic per-channel version sequence
        # (WikiVersionStore._next_version_number). Keyed by ``_id`` == channel_id
        # (NOT ``channel_id``). Purge it so a re-ingested channel restarts
        # numbering from 1 and no channel_id lingers after a hard delete.
        counts["wiki_version_counters"] = await _delete_many(
            self._db["wiki_version_counters"], {"_id": channel_id}
        )
        counts["write_intents"] = await _delete_many(
            self._write_intents, {"channel_id": channel_id}
        )
        counts["pipeline_checkpoints"] = await _delete_many(
            self._pipeline_checkpoints, {"channel_id": channel_id}
        )

        # Re-count then clear sync state + sync jobs via the existing helper so
        # the reset path and the purge path share identical semantics.
        counts["channel_sync_state"] = await self._channel_sync_state.count_documents(
            {"channel_id": channel_id}
        )
        counts["sync_jobs"] = await self._sync_jobs.count_documents({"channel_id": channel_id})
        await self.clear_channel_sync_state(channel_id)

        return counts

    async def log_channel_purge_audit(
        self,
        *,
        channel_id: str,
        principal_id: str,
        counts: dict[str, int],
        errors: dict[str, str],
        unlinked_from: list[str],
        purge_run_id: str,
        ts: datetime | None = None,
    ) -> None:
        """Append one durable audit record for a channel hard-purge run.

        delete-channel-v2 Wave 2. Writes to the RETAINED ``channel_audit_log``
        collection (NEVER touched by :meth:`purge_channel`), so the record of
        what was deleted survives the deletion. Called by the Wave-2 fan-out
        service AFTER all store stages, BEFORE the lock release, so a crash
        between audit and release leaves the lock for the reaper to re-run.

        ``purge_run_id`` (a UUID assigned per ``purge_channel`` invocation)
        distinguishes a reaper re-run of the same channel from the original
        attempt — multiple rows for one ``channel_id`` are expected and each
        carries its own run id (should-fix #6).
        """
        await self._channel_audit_log.insert_one(
            {
                "channel_id": channel_id,
                "principal_id": principal_id,
                "purge_run_id": purge_run_id,
                "counts": counts,
                "errors": errors,
                "unlinked_from": unlinked_from,
                "ts": ts if ts is not None else datetime.now(tz=UTC),
            }
        )

    # ------------------------------------------------------------------
    # Contradiction watermark (P0-1 pipeline-cost-latency-reduction-v2)
    # ------------------------------------------------------------------

    async def get_contradiction_watermark(self, channel_id: str) -> datetime:
        """Return the channel's persisted ``contradiction_watermark``.

        Rows that pre-date the watermark field (i.e. existing pre-deploy
        documents OR brand-new channels with no sync state yet) are
        treated as the Unix epoch ``datetime(1970, 1, 1, tzinfo=UTC)``
        so the very first post-deploy ``check_and_supersede_for_channel``
        call processes every fact written for the channel.

        No schema migration is required — the ``advance_contradiction_watermark``
        ``$lte`` filter combined with this epoch default handles missing
        values uniformly.
        """
        epoch = datetime(1970, 1, 1, tzinfo=UTC)
        doc = await self._channel_sync_state.find_one(
            {"channel_id": channel_id},
            {"contradiction_watermark": 1},
        )
        if doc is None:
            return epoch
        wm = doc.get("contradiction_watermark")
        if wm is None:
            return epoch
        if isinstance(wm, datetime):
            return wm if wm.tzinfo is not None else wm.replace(tzinfo=UTC)
        # Defensive — ISO-8601 string fallback in case a future writer
        # persists the watermark as a string. Pymongo normally stores
        # ``datetime`` natively as BSON Date so this branch is rarely hit.
        if isinstance(wm, str):
            try:
                parsed = datetime.fromisoformat(wm.replace("Z", "+00:00"))
                return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
            except ValueError:
                logger.warning(
                    "get_contradiction_watermark: unparseable string watermark "
                    "channel=%s value=%r — falling back to epoch",
                    channel_id,
                    wm,
                )
        return epoch

    async def advance_contradiction_watermark(
        self,
        channel_id: str,
        pre_check: datetime,
        post_check: datetime,
    ) -> bool:
        """Atomically advance ``contradiction_watermark`` from ``pre_check`` → ``post_check``.

        Uses ``find_one_and_update`` with a ``$lte`` filter on the existing
        watermark to guarantee that two concurrent post-sync checks
        cannot both succeed — the loser observes ``result is None`` and
        the caller treats that as "another invocation already advanced
        the watermark; the work is done".

        The filter accepts either ``contradiction_watermark <= pre_check``
        OR a missing field (existing pre-deploy rows / fresh channels),
        so the first post-deploy call always wins regardless of whether
        the field was ever persisted.

        Returns:
            True when this caller successfully advanced the watermark,
            False when a concurrent caller had already moved it past
            ``pre_check``.
        """
        # Normalise tzinfo so the BSON write is always UTC-aware.
        if pre_check.tzinfo is None:
            pre_check = pre_check.replace(tzinfo=UTC)
        if post_check.tzinfo is None:
            post_check = post_check.replace(tzinfo=UTC)

        result = await self._channel_sync_state.find_one_and_update(
            {
                "channel_id": channel_id,
                "$or": [
                    {"contradiction_watermark": {"$lte": pre_check}},
                    {"contradiction_watermark": {"$exists": False}},
                ],
            },
            {"$set": {"contradiction_watermark": post_check}},
            upsert=False,
            return_document=False,
        )
        return result is not None

    # ------------------------------------------------------------------
    # Channel hard-purge lock (delete-channel-v2 Wave 0)
    # ------------------------------------------------------------------

    async def claim_purge(
        self,
        channel_id: str,
        *,
        stale_after_s: float = PURGE_LOCK_STALE_AFTER_S,
        owner_principal_id: str | None = None,
    ) -> bool:
        """Atomically claim the hard-purge lock for ``channel_id``.

        This is the cross-process CAS gate that prevents two concurrent
        purges (user re-click + reaper, or two EE workers) from both
        running the destructive fan-out and bricking the channel. Modelled
        on :meth:`advance_contradiction_watermark` — a ``find_one_and_update``
        with a ``$or`` filter that grants only when no lock exists OR the
        existing lock is stale (its ``started_at`` is older than
        ``stale_after_s``).

        Returns:
            True  — this call WON the lock (a fresh lock was created or a
                    stale one reclaimed); the caller may run the fan-out.
            False — a fresh (non-stale) lock is already held by another
                    purge; the caller must abort (do NOT run the fan-out).

        Concurrency note: with ``upsert=True`` and a unique index on
        ``channel_id``, two simultaneous inserts (both observing "no doc")
        race — one wins, the other raises ``DuplicateKeyError``. We catch
        that and return False (treat the loser as "lost"). The single-doc
        ``$or`` filter handles the stale-reclaim case without a race because
        ``find_one_and_update`` is atomic at the document level.
        """
        from pymongo import ReturnDocument
        from pymongo.errors import DuplicateKeyError

        now = datetime.now(tz=UTC)
        stale_cutoff = now - timedelta(seconds=stale_after_s)
        set_doc: dict[str, Any] = {"state": "purging", "started_at": now}
        if owner_principal_id is not None:
            set_doc["owner_principal_id"] = owner_principal_id
        try:
            result = await self._channel_purge_locks.find_one_and_update(
                {
                    "channel_id": channel_id,
                    "$or": [
                        {"started_at": {"$lt": stale_cutoff}},
                        {"started_at": {"$exists": False}},
                    ],
                },
                {
                    "$set": set_doc,
                    "$setOnInsert": {"channel_id": channel_id},
                },
                upsert=True,
                return_document=ReturnDocument.AFTER,
            )
        except DuplicateKeyError:
            # Concurrent insert lost the upsert race — a fresh lock now
            # exists and is held by the winner. Treat as "lost".
            return False
        # ``upsert=True`` with ``return_document=AFTER`` always returns the
        # post-image when this call matched or inserted. If the doc existed
        # AND was fresh (failed the $or), Mongo would raise DuplicateKeyError
        # on the upsert attempt (handled above), so a non-None result here
        # means we hold the lock.
        return result is not None

    async def release_purge(self, channel_id: str) -> None:
        """Delete the hard-purge lock doc for ``channel_id`` (terminal success).

        Idempotent — a missing doc is a no-op. Called only after the fan-out
        completes with no errors; on partial failure the lock is RETAINED so
        the reaper re-runs the purge to convergence.
        """
        await self._channel_purge_locks.delete_one({"channel_id": channel_id})

    async def is_purging(
        self,
        channel_id: str,
        *,
        stale_after_s: float = PURGE_LOCK_STALE_AFTER_S,
    ) -> bool:
        """Return True iff a NON-stale purge lock exists for ``channel_id``.

        A stale lock (``started_at`` older than ``stale_after_s``) is treated
        as not-purging so a crashed purge does not wedge a channel's writers
        forever — the reaper will eventually reclaim it.
        """
        now = datetime.now(tz=UTC)
        stale_cutoff = now - timedelta(seconds=stale_after_s)
        doc = await self._channel_purge_locks.find_one(
            {
                "channel_id": channel_id,
                "started_at": {"$gte": stale_cutoff},
            }
        )
        return doc is not None

    async def get_purging_channel_ids(
        self,
        *,
        stale_after_s: float = PURGE_LOCK_STALE_AFTER_S,
    ) -> set[str]:
        """Return the set of channel_ids holding a NON-stale purge lock.

        Used by the ExtractionWorker per-tick to build the ``$nin`` claim
        filter and by other writer guards that need the full active set.
        Stale locks are excluded (same rationale as :meth:`is_purging`).
        """
        now = datetime.now(tz=UTC)
        stale_cutoff = now - timedelta(seconds=stale_after_s)
        ids: set[str] = set()
        async for doc in self._channel_purge_locks.find(
            {"started_at": {"$gte": stale_cutoff}},
            {"channel_id": 1},
        ):
            cid = doc.get("channel_id")
            if cid:
                ids.add(cid)
        return ids

    async def list_stale_purge_locks(self, older_than_s: float) -> list[str]:
        """Return channel_ids whose purge lock is stale (for the reaper).

        A lock is stale when its ``started_at`` is older than
        ``older_than_s`` seconds — i.e. the purge that claimed it likely
        crashed mid-run. The reaper re-invokes ``purge_channel`` for each,
        which re-claims via CAS (idempotent / re-entrant).
        """
        now = datetime.now(tz=UTC)
        stale_cutoff = now - timedelta(seconds=older_than_s)
        ids: list[str] = []
        async for doc in self._channel_purge_locks.find(
            {"started_at": {"$lt": stale_cutoff}},
            {"channel_id": 1},
        ):
            cid = doc.get("channel_id")
            if cid:
                ids.append(cid)
        return ids

    async def count_synced_channels(self) -> int:
        """Return the number of channels that have a sync state record."""
        return await self._channel_sync_state.count_documents({})

    async def list_synced_channel_ids(self) -> list[str]:
        """Return all channel IDs that have a sync state record."""
        ids: list[str] = []
        async for doc in self._channel_sync_state.find({}, {"channel_id": 1}):
            ids.append(doc["channel_id"])
        return ids

    async def get_channel_display_name(self, channel_id: str) -> str | None:
        """Get the display name for a channel from its most recent activity log entry."""
        doc = await self._activity_events.find_one(
            {"channel_id": channel_id, "details.channel_name": {"$exists": True}},
            sort=[("timestamp", -1)],
        )
        if doc:
            return doc.get("details", {}).get("channel_name")
        return None

    async def get_last_sync_timestamp(self) -> str | None:
        """Return the most recent last_sync_ts across all channels, or None."""
        doc = await self._channel_sync_state.find_one({}, sort=[("last_sync_ts", -1)])
        if doc is None:
            return None
        return doc.get("last_sync_ts")

    # ------------------------------------------------------------------
    # Outbox pattern (write intents)
    # ------------------------------------------------------------------

    async def create_write_intent(
        self,
        facts: list[dict[str, Any]],
        entities: list[dict[str, Any]],
        relationships: list[dict[str, Any]],
        channel_id: str | None = None,
    ) -> str:
        """Create a WriteIntent and return its ID.

        ``channel_id`` is the owning channel for this intent and is persisted
        top-level so the channel hard-purge (``delete-channel-v2``) can drop
        the intent in one indexed pass. The persister passes its session-scoped
        ``channel_id`` (extraction batches are per-channel today). Pass ``None``
        when a single intent batches facts from more than one channel — the
        WriteReconciler's per-fact channel filter (Wave 0) handles purge safety
        in that case regardless of the top-level value.
        """
        intent = WriteIntent(
            facts=facts,
            entities=entities,
            relationships=relationships,
            channel_id=channel_id,
        )
        await self._write_intents.insert_one(intent.model_dump())
        return intent.id

    async def mark_intent_weaviate_done(self, intent_id: str) -> None:
        """Mark the Weaviate write as completed for the given intent."""
        await self._write_intents.update_one({"id": intent_id}, {"$set": {"weaviate_done": True}})

    async def mark_intent_neo4j_done(self, intent_id: str) -> None:
        """Mark the Neo4j write as completed for the given intent."""
        await self._write_intents.update_one({"id": intent_id}, {"$set": {"neo4j_done": True}})

    async def mark_intent_complete(self, intent_id: str) -> None:
        """Mark both Weaviate and Neo4j writes as completed for the given intent."""
        await self._write_intents.update_one(
            {"id": intent_id},
            {"$set": {"weaviate_done": True, "neo4j_done": True}},
        )

    async def get_pending_intents(self, max_age_minutes: int = 15) -> list[WriteIntent]:
        """Return intents older than max_age_minutes that are not yet fully complete."""
        cutoff = datetime.now(tz=UTC) - timedelta(minutes=max_age_minutes)
        cursor = self._write_intents.find(
            {
                "created_at": {"$lt": cutoff},
                "$or": [{"weaviate_done": False}, {"neo4j_done": False}],
            }
        )
        intents: list[WriteIntent] = []
        async for doc in cursor:
            doc.pop("_id", None)
            intents.append(WriteIntent(**doc))
        return intents

    # ------------------------------------------------------------------
    # Activity feed
    # ------------------------------------------------------------------

    async def log_activity(
        self,
        event_type: str,
        channel_id: str,
        details: dict[str, Any],
    ) -> None:
        """Insert a new ActivityEvent into the activity feed."""
        event = ActivityEvent(
            event_type=event_type,
            channel_id=channel_id,
            details=details,
        )
        await self._activity_events.insert_one(event.model_dump())

    async def get_recent_activity(self, limit: int = 20) -> list[ActivityEvent]:
        """Return the most recent activity events, newest first."""
        cursor = self._activity_events.find({}, sort=[("timestamp", -1)], limit=limit)
        events: list[ActivityEvent] = []
        async for doc in cursor:
            doc.pop("_id", None)
            events.append(ActivityEvent(**doc))
        return events

    async def get_sync_history(
        self,
        channel_id: str | None = None,
        limit: int = 20,
    ) -> list[ActivityEvent]:
        """Return sync-related activity events with results_summary data."""
        query: dict[str, Any] = {
            "event_type": {"$in": ["sync_completed", "sync_failed"]},
        }
        if channel_id is not None:
            query["channel_id"] = channel_id
        cursor = self._activity_events.find(
            query,
            sort=[("timestamp", -1)],
            limit=limit,
        )
        events: list[ActivityEvent] = []
        async for doc in cursor:
            doc.pop("_id", None)
            events.append(ActivityEvent(**doc))
        return events

    # ------------------------------------------------------------------
    # Channel policies
    # ------------------------------------------------------------------

    async def get_channel_policy(self, channel_id: str) -> ChannelPolicy | None:
        """Return the policy for a channel, or None if not set."""
        doc = await self._channel_policies.find_one({"channel_id": channel_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return ChannelPolicy(**doc)

    async def upsert_channel_policy(self, policy: ChannelPolicy) -> ChannelPolicy:
        """Create or update a channel policy. Returns the persisted policy."""
        policy.updated_at = datetime.now(tz=UTC)
        await self._channel_policies.update_one(
            {"channel_id": policy.channel_id},
            {"$set": policy.model_dump(mode="json")},
            upsert=True,
        )
        return policy

    async def delete_channel_policy(self, channel_id: str) -> bool:
        """Delete a channel policy. Returns True if a document was deleted."""
        result = await self._channel_policies.delete_one({"channel_id": channel_id})
        return result.deleted_count > 0

    async def list_channel_policies(self) -> list[ChannelPolicy]:
        """Return all channel policies."""
        policies: list[ChannelPolicy] = []
        async for doc in self._channel_policies.find():
            doc.pop("_id", None)
            policies.append(ChannelPolicy(**doc))
        return policies

    async def get_global_defaults(self) -> GlobalPolicyDefaults:
        """Return the global policy defaults (always exists after startup)."""
        doc = await self._global_policy_defaults.find_one({"id": "global"})
        if doc is None:
            return GlobalPolicyDefaults()
        doc.pop("_id", None)
        return GlobalPolicyDefaults(**doc)

    async def update_global_defaults(
        self,
        defaults: GlobalPolicyDefaults,
    ) -> GlobalPolicyDefaults:
        """Update the global policy defaults."""
        defaults.updated_at = datetime.now(tz=UTC)
        await self._global_policy_defaults.update_one(
            {"id": "global"},
            {"$set": defaults.model_dump(mode="json")},
            upsert=True,
        )
        return defaults

    async def increment_sync_counter(self, channel_id: str) -> int:
        """Atomically increment syncs_since_last_consolidation. Returns new value.

        Uses upsert so it works even for channels without an explicit policy document.
        """
        result = await self._channel_policies.find_one_and_update(
            {"channel_id": channel_id},
            {
                "$inc": {"syncs_since_last_consolidation": 1},
                "$setOnInsert": {"channel_id": channel_id, "enabled": True},
            },
            upsert=True,
            return_document=True,
        )
        if result is None:
            return 0
        return result.get("syncs_since_last_consolidation", 0)

    async def reset_sync_counter(self, channel_id: str) -> None:
        """Reset syncs_since_last_consolidation to 0."""
        await self._channel_policies.update_one(
            {"channel_id": channel_id},
            {"$set": {"syncs_since_last_consolidation": 0}},
        )

    # ------------------------------------------------------------------
    # Pipeline checkpoints
    # ------------------------------------------------------------------

    async def save_pipeline_checkpoint(
        self,
        sync_job_id: str,
        batch_num: int,
        channel_id: str,
        completed_stage: str,
        completed_stage_index: int,
        state_snapshot: dict[str, Any],
        stage_timings: dict[str, float],
    ) -> None:
        batch_key = f"{sync_job_id}:{batch_num}"
        await self._pipeline_checkpoints.update_one(
            {"batch_key": batch_key},
            {
                "$set": {
                    "batch_key": batch_key,
                    "sync_job_id": sync_job_id,
                    "batch_num": batch_num,
                    "channel_id": channel_id,
                    "completed_stage": completed_stage,
                    "completed_stage_index": completed_stage_index,
                    "state_snapshot": state_snapshot,
                    "stage_timings": stage_timings,
                    "updated_at": datetime.now(tz=UTC),
                },
                "$setOnInsert": {"created_at": datetime.now(tz=UTC)},
            },
            upsert=True,
        )

    async def load_pipeline_checkpoint(
        self, sync_job_id: str, batch_num: int
    ) -> dict[str, Any] | None:
        batch_key = f"{sync_job_id}:{batch_num}"
        doc = await self._pipeline_checkpoints.find_one({"batch_key": batch_key})
        if doc is None:
            return None
        doc.pop("_id", None)
        return doc

    async def delete_pipeline_checkpoint(self, sync_job_id: str, batch_num: int) -> None:
        batch_key = f"{sync_job_id}:{batch_num}"
        await self._pipeline_checkpoints.delete_one({"batch_key": batch_key})

    # ------------------------------------------------------------------
    # Agent model configuration
    # ------------------------------------------------------------------

    async def get_agent_model_config(self) -> dict[str, Any] | None:
        """Load per-agent model configuration from MongoDB."""
        doc = await self.db["agent_model_config"].find_one({"_id": "agent_model_config"})
        if doc:
            doc.pop("_id", None)
        return doc

    async def save_agent_model_config(self, models: dict[str, str]) -> None:
        """Persist per-agent model assignments to MongoDB."""
        from datetime import UTC, datetime

        await self.db["agent_model_config"].update_one(
            {"_id": "agent_model_config"},
            {"$set": {"models": models, "updated_at": datetime.now(tz=UTC).isoformat()}},
            upsert=True,
        )

    # ------------------------------------------------------------------
    # Embedding meta — drives the boot-time dimension guard.
    # ------------------------------------------------------------------
    # Schema:
    #   {
    #     "_id": "embedding_meta",
    #     "provider": "<litellm prefix>",
    #     "model": "<model id>",
    #     "dimensions": <int>,
    #     "last_probe_at": <ISO timestamp>,
    #     "last_probe_ok": <bool>,
    #     "last_probe_error": <str | None>,
    #   }
    #
    # Updated by ``llm.embedding_health.probe_and_validate`` once at boot,
    # and by the re-embed migration after a successful run. The dim guard
    # compares ``dimensions`` against the configured ``EMBEDDING_DIMENSIONS``
    # to refuse boots that would corrupt the existing Weaviate index.

    async def get_embedding_meta(self) -> dict[str, Any] | None:
        """Return the persisted embedding configuration record, or None."""
        doc = await self.db["embedding_meta"].find_one({"_id": "embedding_meta"})
        if doc:
            doc.pop("_id", None)
        return doc

    async def set_embedding_meta(
        self,
        *,
        provider: str,
        model: str,
        dimensions: int,
        ok: bool,
        error: str | None = None,
    ) -> None:
        """Upsert the embedding meta record after a probe / migration."""
        from datetime import UTC, datetime

        await self.db["embedding_meta"].update_one(
            {"_id": "embedding_meta"},
            {
                "$set": {
                    "provider": provider,
                    "model": model,
                    "dimensions": dimensions,
                    "last_probe_at": datetime.now(tz=UTC).isoformat(),
                    "last_probe_ok": ok,
                    "last_probe_error": error,
                }
            },
            upsert=True,
        )

    # ------------------------------------------------------------------
    # Encrypted embedding API key — written by the Settings UI, decrypted
    # only inside the embedding shim immediately before the LiteLLM call.
    # ------------------------------------------------------------------

    async def get_embedding_secret(self) -> dict[str, Any] | None:
        """Return the ciphertext + iv + tag for the stored API key, or None."""
        doc = await self.db["embedding_secret"].find_one({"_id": "embedding_api_key"})
        if doc:
            doc.pop("_id", None)
        return doc

    async def set_embedding_secret(
        self,
        *,
        ciphertext_b64: str,
        iv_b64: str,
        tag_b64: str,
    ) -> None:
        """Persist an encrypted API key — values are pre-encoded base64
        strings so the document JSON-serialises cleanly."""
        from datetime import UTC, datetime

        await self.db["embedding_secret"].update_one(
            {"_id": "embedding_api_key"},
            {
                "$set": {
                    "ciphertext_b64": ciphertext_b64,
                    "iv_b64": iv_b64,
                    "tag_b64": tag_b64,
                    "updated_at": datetime.now(tz=UTC).isoformat(),
                }
            },
            upsert=True,
        )

    async def clear_embedding_secret(self) -> None:
        await self.db["embedding_secret"].delete_one({"_id": "embedding_api_key"})

    # ------------------------------------------------------------------
    # Message Store (channel_messages collection)
    # ------------------------------------------------------------------

    async def upsert_channel_messages(self, messages: list[ChannelMessage]) -> dict[str, int]:
        """Bulk-upsert messages into ``channel_messages``.

        Idempotency contract: calling twice with the same ``(source_id,
        channel_id, message_id)`` yields exactly one document. ``$setOnInsert``
        guards ``extraction_status``, ``attempt_count``, ``next_attempt_at`` so
        re-syncs do NOT reset rows that the worker has already moved past
        ``pending`` (a re-sync should fetch new messages, not re-extract done
        ones).

        Returns a count summary ``{"inserted": int, "modified": int,
        "matched": int, "upserted_ids": int}`` for observability.
        """
        if not messages:
            return {"inserted": 0, "modified": 0, "matched": 0, "upserted_ids": 0}

        from pymongo import UpdateOne

        now = datetime.now(tz=UTC)
        ops: list[UpdateOne] = []
        for msg in messages:
            doc = msg.model_dump(mode="json")
            # Mutable fields ($set) — content can be edited at the source.
            mutable = {
                k: doc[k]
                for k in (
                    "channel_name",
                    "timestamp",
                    "author",
                    "author_name",
                    "author_image",
                    "content",
                    "thread_id",
                    "attachments",
                    "reactions",
                    "reply_count",
                    "is_bot",
                    "links",
                    "raw_metadata",
                    # Discord guild id — needed to build
                    # discord.com/channels/{guild}/{channel}/{message} citation
                    # permalinks. Constant per message, but kept in $set (not
                    # $setOnInsert) so a re-sync BACKFILLS it onto rows that
                    # were stored before this field existed. Empty for
                    # non-Discord platforms.
                    "guild_id",
                )
                if k in doc
            }
            mutable["updated_at"] = now

            # Coerce date-shaped fields back to ``datetime`` so Mongo stores
            # them as BSON dates. ``model_dump(mode="json")`` flattens them
            # to ISO strings, which would break ExtractionWorker's claim
            # filter — the filter compares ``next_attempt_at <= now`` and
            # ``created_at < now - settle`` using ``datetime`` values; a
            # string-vs-date $lte comparison in Mongo silently returns false
            # and rows would sit in ``pending`` forever (no claims, no
            # extraction, no wiki).
            def _to_dt(v: Any) -> datetime:
                if isinstance(v, datetime):
                    return v
                if isinstance(v, str):
                    return datetime.fromisoformat(v.replace("Z", "+00:00"))
                return now

            on_insert = {
                "source_id": doc["source_id"],
                "channel_id": doc["channel_id"],
                "message_id": doc["message_id"],
                "extraction_status": doc.get("extraction_status", "pending"),
                "attempt_count": doc.get("attempt_count", 0),
                "next_attempt_at": _to_dt(doc.get("next_attempt_at", now)),
                "last_error": doc.get("last_error"),
                "created_at": _to_dt(doc.get("created_at", now)),
            }
            ops.append(
                UpdateOne(
                    {
                        "source_id": doc["source_id"],
                        "channel_id": doc["channel_id"],
                        "message_id": doc["message_id"],
                    },
                    {"$set": mutable, "$setOnInsert": on_insert},
                    upsert=True,
                )
            )
        result = await self._channel_messages.bulk_write(ops, ordered=False)
        return {
            "inserted": result.inserted_count,
            "modified": result.modified_count,
            "matched": result.matched_count,
            "upserted_ids": len(result.upserted_ids or {}),
        }

    async def get_channel_messages(
        self,
        channel_id: str,
        limit: int = 50,
        since: datetime | None = None,
        before: str | None = None,
        order: str = "desc",
        source_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read messages for a channel from the durable Message Store.

        ``before`` filters by ``message_id`` strictly less than the cursor (used
        by the existing API contract for keyset pagination). ``order=asc``
        flips the sort. ``source_id`` narrows to one ingestion source — useful
        for distinguishing OpenClaw / Hermes pushes from Slack pulls.
        """
        query: dict[str, Any] = {"channel_id": channel_id}
        if source_id is not None:
            query["source_id"] = source_id
        if since is not None:
            query["timestamp"] = {"$gte": since}
        if before is not None:
            query["message_id"] = {"$lt": before}
        sort_dir = -1 if order == "desc" else 1
        cursor = self._channel_messages.find(query).sort("timestamp", sort_dir).limit(limit)
        rows: list[dict[str, Any]] = []
        async for doc in cursor:
            doc.pop("_id", None)
            rows.append(doc)
        return rows

    async def count_channel_messages_by_status(self, channel_id: str) -> dict[str, int]:
        """Aggregate counts by ``extraction_status`` for one channel.

        Backs the ``GET /api/channels/{id}/extraction-status`` endpoint.
        Returns a dict with keys ``pending``, ``extracting``, ``done``,
        ``failed`` (zero-filled for missing statuses).
        """
        pipeline: list[dict[str, Any]] = [
            {"$match": {"channel_id": channel_id}},
            {"$group": {"_id": "$extraction_status", "n": {"$sum": 1}}},
        ]
        counts: dict[str, int] = {"pending": 0, "extracting": 0, "done": 0, "failed": 0}
        async for row in self._channel_messages.aggregate(pipeline):
            status = row.get("_id")
            if isinstance(status, str) and status in counts:
                counts[status] = int(row.get("n", 0))
        return counts

    async def count_channel_messages_failure_subtypes(
        self,
        channel_id: str,
        max_retries: int,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Split ``failed`` rows into ``retrying`` vs ``abandoned``.

        Phase 3 / Task 4.2.4 — the legacy ``failed`` count conflates two
        very different states: rows the worker will retry shortly (still
        below ``max_retries`` AND ``next_attempt_at`` in the near future)
        versus rows the worker has given up on (``attempt_count >=
        max_retries``). The UI surfaces these as distinct chips so an
        operator knows whether to wait or to act.

        Returns ``{"retrying": int, "abandoned": int}``. The legacy
        ``failed`` field on the response equals ``retrying + abandoned``.
        """
        ref_now = now or datetime.now(tz=UTC)
        pipeline: list[dict[str, Any]] = [
            {
                "$match": {
                    "channel_id": channel_id,
                    "extraction_status": "failed",
                }
            },
            {
                "$group": {
                    "_id": None,
                    "abandoned": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$gte": [
                                        {"$ifNull": ["$attempt_count", 0]},
                                        max_retries,
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                    "retrying": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {
                                            "$lt": [
                                                {"$ifNull": ["$attempt_count", 0]},
                                                max_retries,
                                            ]
                                        },
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$next_attempt_at", ref_now]},
                                                ref_now,
                                            ]
                                        },
                                    ]
                                },
                                1,
                                0,
                            ]
                        }
                    },
                }
            },
        ]
        out: dict[str, int] = {"retrying": 0, "abandoned": 0}
        async for row in self._channel_messages.aggregate(pipeline):
            out["retrying"] = int(row.get("retrying", 0) or 0)
            out["abandoned"] = int(row.get("abandoned", 0) or 0)
            break
        return out

    async def find_channel_message_by_message_id(
        self,
        channel_id: str,
        message_id: str,
        source_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch a single message by its identity.

        Used by the preprocessor (thread-parent lookup) and the coreference
        resolver (adjacent-message context) to replace the prior phantom
        ``raw_messages`` reads.
        """
        query: dict[str, Any] = {"channel_id": channel_id, "message_id": message_id}
        if source_id is not None:
            query["source_id"] = source_id
        doc = await self._channel_messages.find_one(query)
        if doc is None:
            return None
        doc.pop("_id", None)
        return doc

    async def update_channel_message_status(
        self,
        source_id: str,
        channel_id: str,
        message_id: str,
        new_status: str,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> bool:
        """Transition one message's ``extraction_status``.

        Validates the transition against ``EXTRACTION_STATUS_TRANSITIONS`` and
        returns False without mutating the document if the transition is
        illegal. The ExtractionWorker is the primary caller; the sync runner
        does not write status (initial ``pending`` lands via ``$setOnInsert``
        in :meth:`upsert_channel_messages`).
        """
        # Encode the legal-from-states for ``new_status`` directly in the
        # Mongo filter so the validation + write happens as a single atomic
        # operation. A previous read-then-write split lost concurrent
        # transitions: two workers both read ``extracting``, the first wrote
        # ``done``, the second's ``update_one`` (which lacked a status filter)
        # then clobbered ``done`` back to ``failed``.
        allowed_from = {
            state
            for state, allowed in EXTRACTION_STATUS_TRANSITIONS.items()
            if new_status in allowed
        }
        # Treat a no-op transition (``new_status == from_status``) as success
        # — preserves prior behaviour for idempotent retries.
        allowed_from.add(new_status)
        update_set: dict[str, Any] = {
            "extraction_status": new_status,
            "updated_at": datetime.now(tz=UTC),
        }
        if last_error is not None:
            update_set["last_error"] = last_error
        if next_attempt_at is not None:
            update_set["next_attempt_at"] = next_attempt_at
        update_doc: dict[str, Any] = {"$set": update_set}
        if new_status == "failed":
            update_doc["$inc"] = {"attempt_count": 1}
        result = await self._channel_messages.find_one_and_update(
            {
                "source_id": source_id,
                "channel_id": channel_id,
                "message_id": message_id,
                "extraction_status": {"$in": list(allowed_from)},
            },
            update_doc,
        )
        if result is not None:
            return True
        # Filter missed: either the row doesn't exist or it's in a state from
        # which the transition is illegal. A second cheap read disambiguates
        # so ops still get the warning that surfaced the issue before.
        existing = await self._channel_messages.find_one(
            {"source_id": source_id, "channel_id": channel_id, "message_id": message_id},
            projection={"extraction_status": 1},
        )
        if existing is not None:
            logger.warning(
                "channel_messages: rejected illegal transition %s -> %s for %s/%s/%s",
                existing.get("extraction_status", "pending"),
                new_status,
                source_id,
                channel_id,
                message_id,
            )
        return False

    # ------------------------------------------------------------------
    # Message Store: ExtractionWorker primitives
    # ------------------------------------------------------------------

    async def claim_pending_messages_for_extraction(
        self,
        batch_size: int,
        channel_id: str | None = None,
        settle_seconds: int = 5,
        max_retries: int = 5,
        purging_channel_ids: set[str] | list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically claim up to ``batch_size`` pending OR failed-and-due messages.

        Worker queries rows whose ``extraction_status="pending"`` OR
        (``extraction_status="failed"`` AND ``attempt_count < max_retries``),
        ``next_attempt_at <= now``, and ``created_at < now - settle_seconds``
        (the settle window gives bulk upserts a chance to land before the
        worker scans), then flips them to ``"extracting"`` via per-row
        ``find_one_and_update`` so two worker instances cannot pick up the
        same row.

        ``failed`` rows whose ``next_attempt_at`` has elapsed AND whose
        ``attempt_count`` is below ``max_retries`` are also eligible. This
        is the auto-retry path — combined with the content-hash deterministic
        fact ID, retries do not produce phantom Weaviate duplicates. Rows
        that exhaust their retry budget stay ``failed`` permanently.

        Returns the claimed documents (with ``_id`` stripped) — possibly
        fewer than ``batch_size`` if the queue is short. Each returned
        document carries the post-update state (status="extracting").

        Per-row ``find_one_and_update`` is intentional rather than a single
        ``update_many``: ``update_many`` is atomic at the document level but
        does not return the matched documents, so the worker would still
        need a follow-up read that races with other workers. The N
        round-trips are acceptable at OSS scale (batch_size ≤ 32) and avoid
        the race entirely.
        """
        from pymongo import ReturnDocument

        now = datetime.now(tz=UTC)
        # ``$or`` lets a single claim cycle drain both fresh pending rows
        # AND failed-but-eligible-for-retry rows. The state-machine
        # transitions encoded in ``EXTRACTION_STATUS_TRANSITIONS`` allow
        # ``failed → pending``, but the worker takes the shortcut of going
        # straight to ``extracting`` here because the row is being actively
        # reclaimed (the brief intermediate state is invisible to readers —
        # the find_one_and_update is atomic).
        filter_doc: dict[str, Any] = {
            "$or": [
                {"extraction_status": "pending"},
                {
                    "extraction_status": "failed",
                    "attempt_count": {"$lt": max_retries},
                },
            ],
            "next_attempt_at": {"$lte": now},
            "created_at": {"$lt": now - timedelta(seconds=settle_seconds)},
        }
        # delete-channel-v2 Wave 0 — writer guard at the CLAIM. The periodic
        # tick passes ``channel_id=None`` and drains ALL channels, so a
        # per-channel guard is insufficient; the worker pre-fetches the
        # purging set once per tick and passes it here as a ``$nin`` so a
        # purge in flight can never have its rows re-claimed (and thus
        # re-extracted into Weaviate/graph) by the global drain.
        purging = set(purging_channel_ids or ())
        if channel_id is not None:
            # Explicit single-channel scope (manual "extract now"). If that
            # very channel is purging, claim nothing rather than racing the
            # purge fan-out.
            if channel_id in purging:
                return []
            filter_doc["channel_id"] = channel_id
        elif purging:
            # Global drain — exclude every purging channel.
            filter_doc["channel_id"] = {"$nin": list(purging)}
        # ``attempt_count`` is intentionally NOT reset on the
        # failed → extracting shortcut. The total attempts encode the
        # retry budget (capped by ``max_retries``) and the worker's
        # backoff schedule expects monotonic counts.
        update_doc = {
            "$set": {
                "extraction_status": "extracting",
                "updated_at": now,
            }
        }
        claimed: list[dict[str, Any]] = []
        for _ in range(batch_size):
            doc = await self._channel_messages.find_one_and_update(
                filter_doc,
                update_doc,
                return_document=ReturnDocument.AFTER,
                sort=[("next_attempt_at", 1)],
            )
            if doc is None:
                break
            doc.pop("_id", None)
            claimed.append(doc)
        return claimed

    async def finalize_extraction_status_bulk(
        self,
        keys: list[tuple[str, str, str]],
        new_status: str,
        last_error: str | None = None,
        next_attempt_at: datetime | None = None,
    ) -> int:
        """Bulk-transition many ``(source_id, channel_id, message_id)`` rows
        to ``new_status`` (typically ``"done"`` or ``"failed"``).

        Validates the source state via ``EXTRACTION_STATUS_TRANSITIONS`` —
        only rows currently in a state from which ``new_status`` is
        reachable are updated; mismatched rows are silently skipped (the
        worker logs a warning per skip via the per-row helper). Returns
        the count of rows actually mutated. ``attempt_count`` is
        incremented when transitioning to ``"failed"``.

        Used by :class:`ExtractionWorker` after a batch completes so the N
        per-row ``update_one`` round-trips collapse into a single
        ``bulk_write``.
        """
        if not keys:
            return 0
        from pymongo import UpdateOne

        # Determine the set of allowed source states for this transition.
        # Do NOT permit self-transitions (``new_status == from_state``).
        # The ``EXTRACTION_STATUS_TRANSITIONS`` map encodes ``done -> done``
        # as forbidden so a re-extraction must go through
        # ``failed -> pending -> extracting -> done`` and not silently skip
        # a step. Idempotency is provided by ``$setOnInsert`` at upsert
        # time, not by self-transitions here.
        allowed_from = {
            from_state
            for from_state, allowed in EXTRACTION_STATUS_TRANSITIONS.items()
            if new_status in allowed
        }
        if not allowed_from:
            logger.warning(
                "channel_messages: bulk transition to %r has no valid source state",
                new_status,
            )
            return 0
        now = datetime.now(tz=UTC)
        ops: list[UpdateOne] = []
        for source_id, channel_id, message_id in keys:
            update_set: dict[str, Any] = {
                "extraction_status": new_status,
                "updated_at": now,
            }
            if last_error is not None:
                update_set["last_error"] = last_error
            if next_attempt_at is not None:
                update_set["next_attempt_at"] = next_attempt_at
            update: dict[str, Any] = {"$set": update_set}
            if new_status == "failed":
                update["$inc"] = {"attempt_count": 1}
            ops.append(
                UpdateOne(
                    {
                        "source_id": source_id,
                        "channel_id": channel_id,
                        "message_id": message_id,
                        "extraction_status": {"$in": list(allowed_from)},
                    },
                    update,
                )
            )
        result = await self._channel_messages.bulk_write(ops, ordered=False)
        return result.modified_count

    # ------------------------------------------------------------------
    # Push-source registry
    # ------------------------------------------------------------------

    async def get_external_source(self, source_id: str) -> ExternalSource | None:
        """Fetch the registered external source by id, or None if missing.

        Used by the HMAC verifier on every push-event request to look
        up the secret hash + the allowed-channels glob.
        """
        doc = await self._external_sources.find_one({"source_id": source_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return ExternalSource.model_validate(doc)

    async def upsert_external_source(self, source: ExternalSource) -> None:
        """Register or rotate an external source.

        Auto-derives ``secret_fingerprint`` from ``secret`` so callers
        don't have to. Sets ``rotated_at`` on every update so any
        in-flight signatures with a previous secret fail validation
        immediately on the next request.

        ``ExternalSource.secret`` carries ``Field(exclude=True)`` so it
        does NOT appear in ``model_dump()``. The persistence path explicitly
        re-adds the secret AFTER the dump so it lands in MongoDB (HMAC
        verification needs the plaintext) — defense in depth: the model-level
        exclude protects API serialization, the explicit re-add here protects
        the storage path. Removing either is a regression.
        """
        from beever_atlas.services.push_hmac import hash_secret

        doc = source.model_dump(mode="json")
        # Re-add the secret after dump (Field(exclude=True) stripped it).
        doc["secret"] = source.secret
        doc["secret_fingerprint"] = hash_secret(source.secret)
        existing = await self._external_sources.find_one({"source_id": source.source_id})
        if existing is not None:
            doc["rotated_at"] = datetime.now(tz=UTC).isoformat()
        await self._external_sources.update_one(
            {"source_id": source.source_id},
            {"$set": doc},
            upsert=True,
        )

    async def delete_external_source(self, source_id: str) -> bool:
        result = await self._external_sources.delete_one({"source_id": source_id})
        return bool(result.deleted_count)

    async def list_external_sources(self) -> list[ExternalSource]:
        """Enumerate registered external sources for the admin UI.

        Plaintext secrets are filtered out by the ``ExternalSource`` model
        (``Field(exclude=True)``), so it is safe to serialize the returned
        objects to admin responses. The plaintext is only ever returned
        once on creation/rotation by the admin handler, never on list.
        """
        cursor = self._external_sources.find({}).sort("source_id", 1)
        sources: list[ExternalSource] = []
        async for doc in cursor:
            doc.pop("_id", None)
            sources.append(ExternalSource.model_validate(doc))
        return sources

    async def count_idempotency_replays_for_source(self, source_id: str) -> int:
        """Approximate replay count for a source over the active window.

        The ``idempotency_keys`` collection auto-expires via TTL after 24h,
        so a count today is the count over the last 24h. Returned in the
        admin list so operators can spot a noisy / flapping source.
        """
        return int(await self._idempotency_keys.count_documents({"source_id": source_id}))

    async def list_failed_channel_messages(
        self,
        channel_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Paginated read of ``channel_messages`` rows with ``extraction_status="failed"``.

        Sorted + cursor-paginated by ``message_id`` (which is unique within
        a channel via the compound key). The UI displays ``next_attempt_at``
        as a column and may sort client-side; using ``message_id`` as the
        single sort key keeps keyset pagination correct (an earlier draft
        sorted by ``next_attempt_at`` with a ``message_id`` cursor, which
        skipped/duplicated rows when the two were not monotonically
        correlated — caught by code review).
        Returns ``(rows, next_cursor)``; ``next_cursor`` is None on the
        final page.
        """
        query: dict[str, Any] = {
            "channel_id": channel_id,
            "extraction_status": "failed",
        }
        if cursor:
            query["message_id"] = {"$gt": cursor}

        rows: list[dict[str, Any]] = []
        async for doc in self._channel_messages.find(
            query,
            projection={
                "_id": 0,
                "message_id": 1,
                "next_attempt_at": 1,
                "attempt_count": 1,
                "last_error": 1,
            },
            sort=[("message_id", 1)],
            limit=limit + 1,  # fetch one extra to detect end-of-page
        ):
            rows.append(doc)

        next_cursor: str | None = None
        if len(rows) > limit:
            extra = rows.pop()
            next_cursor = str(extra.get("message_id") or "")
        return rows, next_cursor

    # ------------------------------------------------------------------
    # Idempotency replay cache
    # ------------------------------------------------------------------

    async def get_idempotency_record(
        self, source_id: str, idempotency_key: str
    ) -> IdempotencyKeyRecord | None:
        """Fetch a cached response for a previous request, if any.

        The TTL index drops these after 24h. Within that window, the
        same ``(source_id, idempotency_key)`` returns the cached 202
        without re-processing the events — protecting against retries
        that would otherwise re-deliver the same batch.
        """
        doc = await self._idempotency_keys.find_one(
            {"source_id": source_id, "idempotency_key": idempotency_key}
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return IdempotencyKeyRecord.model_validate(doc)

    async def reserve_idempotency_record(
        self, source_id: str, idempotency_key: str, response: dict[str, Any]
    ) -> bool:
        """Record an idempotency reservation. Returns True if the record
        was newly inserted; False if an earlier insert won the race
        (caller should fetch the cached response in that case).

        The compound unique index makes this atomic — two concurrent
        callers cannot both insert; the loser raises DuplicateKeyError
        which we catch and return False from.
        """
        from pymongo.errors import DuplicateKeyError

        try:
            await self._idempotency_keys.insert_one(
                {
                    "source_id": source_id,
                    "idempotency_key": idempotency_key,
                    "response": response,
                    "created_at": datetime.now(tz=UTC),
                }
            )
            return True
        except DuplicateKeyError:
            return False

    # ------------------------------------------------------------------
    # Wiki drift reports (close-the-soak-loop §3)
    # ------------------------------------------------------------------

    async def insert_wiki_drift_report(self, report: Any) -> None:
        """Persist one ``DriftReport`` to ``wiki_drift_reports``.

        Accepts a dataclass instance or a plain dict (the comparator
        always passes the dataclass; tests sometimes pass dicts).
        Adds an inserted ``ts`` field used by the TTL index + the
        per-channel summary aggregation.
        """
        from dataclasses import asdict, is_dataclass

        if is_dataclass(report) and not isinstance(report, type):
            doc: dict[str, Any] = asdict(report)
        elif isinstance(report, dict):
            doc = dict(report)
        else:
            # Defensive: best-effort attribute scrape so a future Pydantic
            # rework doesn't silently drop persistence.
            doc = {
                k: getattr(report, k)
                for k in (
                    "channel_id",
                    "page_id",
                    "levenshtein_title",
                    "levenshtein_section_max",
                    "levenshtein_section_p50",
                    "levenshtein_section_p95",
                    "section_id_jaccard",
                    "incremental_ms",
                    "regenerate_ms",
                    "incremental_section_count",
                    "regenerate_section_count",
                    # ``wiki-llm-native-redesign`` §8.2 — kind facet
                    "kind",
                )
                if hasattr(report, k)
            }
        doc["ts"] = datetime.now(tz=UTC)
        await self._wiki_drift_reports.insert_one(doc)

    async def aggregate_wiki_drift_summary(self, days: int) -> list[dict[str, Any]]:
        """Aggregate ``wiki_drift_reports`` over the last ``days`` days.

        Returns one row per channel with median + p95 medians of the
        Levenshtein section metrics. The pass criterion + freshness
        evaluation lives at the API layer — this method is the data fan-in
        only. ``days`` is treated as inclusive of any rows whose ``ts``
        lies within the window.
        """
        import statistics

        cutoff = datetime.now(tz=UTC) - timedelta(days=max(1, days))
        # Group by both channel_id AND kind so the per-kind facets (added
        # by the wiki-llm-native-redesign §8.3 ship) come out of the same
        # pipeline pass — one extra dimension on the group key, no extra
        # round-trip. Channel-level totals are derived in the Python
        # post-aggregate so the per-kind buckets always sum to the
        # channel total.
        pipeline: list[dict[str, Any]] = [
            {"$match": {"ts": {"$gte": cutoff}}},
            {
                "$group": {
                    "_id": {
                        "channel_id": "$channel_id",
                        "kind": {"$ifNull": ["$kind", ""]},
                    },
                    "page_count": {"$sum": 1},
                    "section_p50_values": {"$push": "$levenshtein_section_p50"},
                    "section_p95_values": {"$push": "$levenshtein_section_p95"},
                    "last_run_ts": {"$max": "$ts"},
                }
            },
        ]
        # Channel → {totals, per_kind: {kind: {p50_median, p95_median, page_count, last_run_ts}}}
        per_channel: dict[str, dict[str, Any]] = {}
        async for row in self._wiki_drift_reports.aggregate(pipeline):
            key = row.get("_id") or {}
            channel_id = str(key.get("channel_id") or "") if isinstance(key, dict) else str(key)
            kind = str(key.get("kind") or "") if isinstance(key, dict) else ""
            # Defensive None-guard — DriftReport's dataclass schema today
            # guarantees these fields are floats, but partial-failure
            # writes or a future schema migration could land docs with
            # missing fields. Filter so the float() coercion never raises
            # TypeError mid-aggregate (would 500 the dashboard).
            p50s = [float(v) for v in (row.get("section_p50_values") or []) if v is not None]
            p95s = [float(v) for v in (row.get("section_p95_values") or []) if v is not None]
            p50_median = statistics.median(p50s) if p50s else 0.0
            p95_median = statistics.median(p95s) if p95s else 0.0
            page_count = int(row.get("page_count", 0) or 0)
            last_run_ts = row.get("last_run_ts")

            entry = per_channel.setdefault(
                channel_id,
                {
                    "channel_id": channel_id,
                    "page_count": 0,
                    "all_p50_values": [],
                    "all_p95_values": [],
                    "per_kind": {},
                    "last_run_ts": None,
                },
            )
            entry["page_count"] += page_count
            entry["all_p50_values"].extend(p50s)
            entry["all_p95_values"].extend(p95s)
            entry["per_kind"][kind] = {
                "kind": kind,
                "page_count": page_count,
                "levenshtein_section_p50_median": p50_median,
                "levenshtein_section_p95_median": p95_median,
                "last_run_ts": last_run_ts,
            }
            current_last = entry["last_run_ts"]
            if last_run_ts is not None and (current_last is None or last_run_ts > current_last):
                entry["last_run_ts"] = last_run_ts

        out: list[dict[str, Any]] = []
        for entry in per_channel.values():
            p50s = entry.pop("all_p50_values")
            p95s = entry.pop("all_p95_values")
            entry["levenshtein_section_p50_median"] = statistics.median(p50s) if p50s else 0.0
            entry["levenshtein_section_p95_median"] = statistics.median(p95s) if p95s else 0.0
            out.append(entry)
        return out

    async def sweep_stale_extracting(self, stale_seconds: int = 600) -> int:
        """Reset rows stuck in ``"extracting"`` for more than ``stale_seconds``
        back to ``"pending"`` so a future tick can re-claim them.

        Recovery path for worker crashes mid-batch (design D6). Default
        10-minute stale window is conservative — extraction batches
        typically complete in 30-90 seconds.
        """
        now = datetime.now(tz=UTC)
        threshold = now - timedelta(seconds=stale_seconds)
        result = await self._channel_messages.update_many(
            {
                "extraction_status": "extracting",
                "updated_at": {"$lt": threshold},
            },
            {
                "$set": {
                    "extraction_status": "pending",
                    "updated_at": now,
                    "next_attempt_at": now,
                }
            },
        )
        if result.modified_count:
            logger.warning(
                "ExtractionWorker: swept %d stale-extracting rows older than %ds",
                result.modified_count,
                stale_seconds,
            )
        return result.modified_count

    # ------------------------------------------------------------------
    # memory-then-wiki-pipeline-realignment — wiki_dirty_queue
    # ------------------------------------------------------------------

    async def enqueue_dirty(
        self,
        channel_id: str,
        page_id: str,
        fact_ids: list[str],
    ) -> None:
        """Append ``fact_ids`` to the per-page dirty row, upserting if absent.

        Uses ``$addToSet`` so duplicate fact_ids never accumulate. On
        insert, status is ``pending`` and ``created_at`` is stamped.
        On every call ``updated_at`` is bumped. If the row was previously
        ``done`` (last flush finished cleanly), the upsert flips it back
        to ``pending`` — the same page can re-enter the queue when new
        facts arrive after a successful flush.
        """
        if not fact_ids:
            return
        now = datetime.now(tz=UTC)
        await self._wiki_dirty_queue.update_one(
            {"channel_id": channel_id, "page_id": page_id},
            {
                "$addToSet": {"fact_ids": {"$each": list(set(fact_ids))}},
                "$set": {
                    "status": "pending",
                    "updated_at": now,
                },
                "$setOnInsert": {
                    "created_at": now,
                },
            },
            upsert=True,
        )

    async def claim_dirty(self, channel_id: str) -> list[dict[str, Any]]:
        """Atomically claim every pending dirty row for the channel.

        Flips ``status: pending → flushing`` for all matching rows in one
        update_many, then returns the rows with their ``_id``, ``page_id``,
        ``fact_ids``. Idempotent — a second concurrent call sees nothing
        to claim and returns an empty list.
        """
        now = datetime.now(tz=UTC)
        await self._wiki_dirty_queue.update_many(
            {"channel_id": channel_id, "status": "pending"},
            {"$set": {"status": "flushing", "updated_at": now}},
        )
        cursor = self._wiki_dirty_queue.find(
            {"channel_id": channel_id, "status": "flushing"},
            {"_id": 1, "page_id": 1, "fact_ids": 1},
        )
        claimed: list[dict[str, Any]] = []
        async for doc in cursor:
            claimed.append(doc)
        return claimed

    async def mark_dirty_done(self, doc_ids: list[Any]) -> None:
        """Flip the supplied dirty-queue rows to ``status="done"``."""
        if not doc_ids:
            return
        now = datetime.now(tz=UTC)
        await self._wiki_dirty_queue.update_many(
            {"_id": {"$in": doc_ids}},
            {"$set": {"status": "done", "updated_at": now}},
        )

    async def recover_stale_flushing(self, stale_seconds: int = 600) -> int:
        """Reset rows stuck in ``flushing`` longer than ``stale_seconds``
        back to ``pending`` so the next flush re-claims them.

        Crash recovery for maintainer-mid-flush failures. Default 10 min.
        Returns the number of rows reset.
        """
        now = datetime.now(tz=UTC)
        threshold = now - timedelta(seconds=stale_seconds)
        result = await self._wiki_dirty_queue.update_many(
            {"status": "flushing", "updated_at": {"$lt": threshold}},
            {"$set": {"status": "pending", "updated_at": now}},
        )
        if result.modified_count:
            logger.warning(
                "WikiMaintainer: recovered %d stale-flushing dirty rows older than %ds",
                result.modified_count,
                stale_seconds,
            )
        return result.modified_count

    async def cleanup_done_dirty(self, retention_days: int = 7) -> int:
        """Delete ``done`` rows older than ``retention_days``.

        Bounds the collection size while keeping recent audit history
        for the operator console.
        """
        cutoff = datetime.now(tz=UTC) - timedelta(days=retention_days)
        result = await self._wiki_dirty_queue.delete_many(
            {"status": "done", "updated_at": {"$lt": cutoff}}
        )
        if result.deleted_count:
            logger.info(
                "WikiMaintainer: cleaned %d done dirty rows older than %dd",
                result.deleted_count,
                retention_days,
            )
        return result.deleted_count
