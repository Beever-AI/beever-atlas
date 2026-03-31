"""Persistence models: MongoDB sync state and outbox."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class SyncJob(BaseModel):
    """Tracks a channel sync job in MongoDB."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    channel_id: str
    status: str = "running"  # running | completed | failed
    sync_type: str = "full"  # full | incremental
    total_messages: int = 0
    processed_messages: int = 0
    current_batch: int = 0
    current_stage: str | None = None
    batch_size: int = 50
    errors: list[str] = Field(default_factory=list)
    stage_timings: dict[str, float] = Field(default_factory=dict)
    stage_details: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    completed_at: datetime | None = None


class ChannelSyncState(BaseModel):
    """Persistent sync state per channel in MongoDB."""

    channel_id: str
    last_sync_ts: str  # ISO-8601 timestamp of last synced message
    total_synced_messages: int = 0


class WriteIntent(BaseModel):
    """Outbox pattern: a pending write intent in MongoDB."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    facts: list[dict[str, Any]] = Field(default_factory=list)
    entities: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    weaviate_done: bool = False
    neo4j_done: bool = False
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class ActivityEvent(BaseModel):
    """An activity event for the dashboard feed."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    event_type: str  # sync_complete, sync_failed, new_entity
    channel_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
