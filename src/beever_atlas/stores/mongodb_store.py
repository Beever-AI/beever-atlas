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


class MongoDBStore:
    """Manages MongoDB collections for Beever Atlas."""

    def __init__(self, uri: str, db_name: str = "beever_atlas") -> None:
        self._client: AsyncIOMotorClient = AsyncIOMotorClient(uri)
        self._db = self._client[db_name]
        self._sync_jobs = self._db["sync_jobs"]
        self._channel_sync_state = self._db["channel_sync_state"]
        self._write_intents = self._db["write_intents"]
        self._activity_events = self._db["activity_events"]

    async def startup(self) -> None:
        """Ping MongoDB to verify the connection is alive."""
        await self._client.admin.command("ping")
        await self._sync_jobs.create_index([("channel_id", 1), ("started_at", -1)])
        await self._write_intents.create_index([("created_at", 1), ("weaviate_done", 1), ("neo4j_done", 1)])
        await self._channel_sync_state.create_index("channel_id", unique=True)
        await self._activity_events.create_index([("timestamp", -1)])

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
        batch_size: int = 50,
    ) -> SyncJob:
        """Create and persist a new SyncJob, returning the model."""
        job = SyncJob(
            channel_id=channel_id,
            sync_type=sync_type,
            total_messages=total_messages,
            batch_size=batch_size,
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
    ) -> None:
        """Update processed message count, current batch index, and optional stage."""
        update: dict[str, Any] = {
            "processed_messages": processed,
            "current_batch": current_batch,
        }
        if current_stage is not None:
            update["current_stage"] = current_stage
        if stage_timings is not None:
            update["stage_timings"] = stage_timings
        if stage_details is not None:
            update["stage_details"] = stage_details
        await self._sync_jobs.update_one(
            {"id": job_id},
            {"$set": update},
        )

    async def complete_sync_job(
        self,
        job_id: str,
        status: str,
        errors: list[str] | None = None,
    ) -> None:
        """Mark a sync job as completed or failed with an optional error list."""
        update: dict[str, Any] = {
            "status": status,
            "completed_at": datetime.now(tz=UTC),
        }
        if errors is not None:
            update["errors"] = errors
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

    async def get_channel_sync_state(self, channel_id: str) -> ChannelSyncState | None:
        """Return the ChannelSyncState for the given channel, or None."""
        doc = await self._channel_sync_state.find_one({"channel_id": channel_id})
        if doc is None:
            return None
        doc.pop("_id", None)
        return ChannelSyncState(**doc)

    async def update_channel_sync_state(
        self,
        channel_id: str,
        last_sync_ts: str,
        increment: int = 0,
    ) -> None:
        """Upsert the channel sync state, optionally incrementing message count."""
        update: dict[str, Any] = {"$set": {"last_sync_ts": last_sync_ts}}
        if increment:
            update["$inc"] = {"total_synced_messages": increment}
        await self._channel_sync_state.update_one(
            {"channel_id": channel_id},
            update,
            upsert=True,
        )

    async def count_synced_channels(self) -> int:
        """Return the number of channels that have a sync state record."""
        return await self._channel_sync_state.count_documents({})

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
