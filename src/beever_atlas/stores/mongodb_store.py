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
        """
        update: dict[str, Any] = {
            "total_messages": total_messages,
            "parent_messages": parent_messages,
        }
        if sync_type is not None:
            update["sync_type"] = sync_type
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
        """
        tagged_entry = {**entry, "batch_idx": batch_idx}
        await self._sync_jobs.update_one(
            {"id": job_id},
            {
                "$push": {
                    "stage_details.activity_log": {
                        "$each": [tagged_entry],
                        "$slice": -50,
                    }
                },
                "$inc": {"version": 1},
            },
        )

    async def increment_batches_completed(self, job_id: str) -> None:
        """Atomic increment of batches_completed — safe under concurrent batch runs."""
        await self._sync_jobs.update_one(
            {"id": job_id},
            {"$inc": {"batches_completed": 1, "version": 1}},
        )

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
        """Return the most recent SyncJob for the given channel, or None."""
        doc = await self._sync_jobs.find_one(
            {"channel_id": channel_id},
            sort=[("started_at", -1)],
        )
        if doc is None:
            return None
        doc.pop("_id", None)
        return SyncJob(**doc)

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
    ) -> str:
        """Create a WriteIntent and return its ID."""
        intent = WriteIntent(facts=facts, entities=entities, relationships=relationships)
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
        if channel_id is not None:
            filter_doc["channel_id"] = channel_id
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
