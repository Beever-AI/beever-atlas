"""MongoDB store client using motor async driver."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from beever_atlas.models import (
    ActivityEvent,
    ChannelSyncState,
    SyncJob,
    WriteIntent,
)
from beever_atlas.models.sync_policy import (
    ChannelPolicy,
    GlobalPolicyDefaults,
)


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

    @property
    def db(self):
        """Expose the underlying AsyncIOMotorDatabase for stores that need it."""
        return self._db

    async def startup(self) -> None:
        """Ping MongoDB to verify the connection is alive."""
        await self._client.admin.command("ping")
        await self._sync_jobs.create_index([("channel_id", 1), ("started_at", -1)])
        await self._write_intents.create_index([("created_at", 1), ("weaviate_done", 1), ("neo4j_done", 1)])
        await self._channel_sync_state.create_index("channel_id", unique=True)
        await self._activity_events.create_index([("timestamp", -1)])
        await self._channel_policies.create_index("channel_id", unique=True)
        await self._pipeline_checkpoints.create_index("batch_key", unique=True)
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
    ) -> None:
        """Mark a sync job as completed or failed with an optional error list."""
        update: dict[str, Any] = {
            "status": status,
            "completed_at": datetime.now(tz=UTC),
        }
        if errors is not None:
            update["errors"] = errors
        if failed_stage is not None:
            update["current_stage"] = failed_stage
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
                    failed.append({
                        "batch_num": batch_result.get("batch_num"),
                        **fact,
                    })
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
        cursor = self._channel_sync_state.find(
            {"channel_id": {"$in": list(channel_ids)}}
        )
        async for doc in cursor:
            cid = doc.get("channel_id")
            doc.pop("_id", None)
            if cid:
                try:
                    result[cid] = ChannelSyncState(**doc)
                except Exception:  # noqa: BLE001
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
        doc = await self._channel_sync_state.find_one(
            {}, sort=[("last_sync_ts", -1)]
        )
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
        await self._write_intents.update_one(
            {"id": intent_id}, {"$set": {"weaviate_done": True}}
        )

    async def mark_intent_neo4j_done(self, intent_id: str) -> None:
        """Mark the Neo4j write as completed for the given intent."""
        await self._write_intents.update_one(
            {"id": intent_id}, {"$set": {"neo4j_done": True}}
        )

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
        cursor = self._activity_events.find(
            {}, sort=[("timestamp", -1)], limit=limit
        )
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
            query, sort=[("timestamp", -1)], limit=limit,
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
        self, defaults: GlobalPolicyDefaults,
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
        self, sync_job_id: str, batch_num: int, channel_id: str,
        completed_stage: str, completed_stage_index: int,
        state_snapshot: dict[str, Any], stage_timings: dict[str, float],
    ) -> None:
        batch_key = f"{sync_job_id}:{batch_num}"
        await self._pipeline_checkpoints.update_one(
            {"batch_key": batch_key},
            {"$set": {
                "batch_key": batch_key, "sync_job_id": sync_job_id,
                "batch_num": batch_num, "channel_id": channel_id,
                "completed_stage": completed_stage,
                "completed_stage_index": completed_stage_index,
                "state_snapshot": state_snapshot,
                "stage_timings": stage_timings,
                "updated_at": datetime.now(tz=UTC),
            }, "$setOnInsert": {"created_at": datetime.now(tz=UTC)}},
            upsert=True,
        )

    async def load_pipeline_checkpoint(self, sync_job_id: str, batch_num: int) -> dict[str, Any] | None:
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
