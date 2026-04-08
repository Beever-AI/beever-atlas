"""Per-channel sync and pipeline policy models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from enum import Enum
from pydantic import BaseModel, Field


class SyncTriggerMode(str, Enum):
    """How ingestion is triggered for a channel."""

    MANUAL = "manual"
    INTERVAL = "interval"  # every N minutes
    CRON = "cron"  # cron expression
    WEBHOOK = "webhook"  # event-driven (future)


class ConsolidationStrategy(str, Enum):
    """When to run consolidation (facts → topics → summaries)."""

    AFTER_EVERY_SYNC = "after_every_sync"
    AFTER_N_SYNCS = "after_n_syncs"
    SCHEDULED = "scheduled"  # independent cron
    MANUAL = "manual"


class SyncConfig(BaseModel):
    """When & how to fetch and ingest messages. All fields optional — None = inherit."""

    trigger_mode: SyncTriggerMode | None = None
    cron_expression: str | None = None  # e.g. "0 */6 * * *"
    interval_minutes: int | None = None  # e.g. 360
    sync_type: str | None = None  # "auto" | "full" | "incremental"
    max_messages: int | None = None
    min_sync_interval_minutes: int | None = None  # cooldown between syncs


class IngestionConfig(BaseModel):
    """How to process messages → Tier 2 facts. All fields optional."""

    batch_size: int | None = None
    quality_threshold: float | None = None
    max_facts_per_message: int | None = None
    skip_entity_extraction: bool | None = None
    skip_graph_writes: bool | None = None


class ConsolidationConfig(BaseModel):
    """When & how to promote Tier 2 → Tier 1 → Tier 0. All fields optional."""

    strategy: ConsolidationStrategy | None = None
    after_n_syncs: int | None = None  # for AFTER_N_SYNCS strategy
    cron_expression: str | None = None  # for SCHEDULED strategy
    similarity_threshold: float | None = None  # cluster assignment
    merge_threshold: float | None = None  # cluster merging
    min_facts_for_clustering: int | None = None  # skip if fewer unclustered facts
    staleness_refresh_days: int | None = None  # auto-refresh stale clusters


class WikiGenerationStrategy(str, Enum):
    """When to regenerate the wiki."""

    AFTER_EVERY_SYNC = "after_every_sync"
    AFTER_CONSOLIDATION = "after_consolidation"
    SCHEDULED = "scheduled"  # independent cron
    MANUAL = "manual"


class WikiConfig(BaseModel):
    """Wiki generation settings. All fields optional — None = inherit."""

    enabled: bool | None = None  # whether wiki generation is enabled for this channel
    generation_strategy: WikiGenerationStrategy | None = None
    cron_expression: str | None = None  # for SCHEDULED strategy
    auto_regenerate_on_stale: bool | None = None  # auto-regen when wiki marked stale
    min_facts_for_generation: int | None = None  # minimum facts needed to generate wiki
    topic_subpage_threshold: int | None = None  # min facts per cluster for sub-pages (default 15)


class ChannelPolicy(BaseModel):
    """Per-channel policy document stored in MongoDB."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    channel_id: str
    connection_id: str | None = None
    preset: str | None = None  # "real-time" | "daily-digest" | "lightweight" | "manual" | "custom"
    sync: SyncConfig = Field(default_factory=SyncConfig)
    ingestion: IngestionConfig = Field(default_factory=IngestionConfig)
    consolidation: ConsolidationConfig = Field(default_factory=ConsolidationConfig)
    wiki: WikiConfig = Field(default_factory=WikiConfig)
    enabled: bool = True
    syncs_since_last_consolidation: int = 0
    created_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class GlobalPolicyDefaults(BaseModel):
    """Single-document global defaults — final fallback for unset fields."""

    id: str = "global"
    sync: SyncConfig = Field(
        default_factory=lambda: SyncConfig(
            trigger_mode=SyncTriggerMode.MANUAL,
            sync_type="auto",
            max_messages=1000,
            min_sync_interval_minutes=5,
        )
    )
    ingestion: IngestionConfig = Field(
        default_factory=lambda: IngestionConfig(
            batch_size=10,
            quality_threshold=0.5,
            max_facts_per_message=2,
            skip_entity_extraction=False,
            skip_graph_writes=False,
        )
    )
    consolidation: ConsolidationConfig = Field(
        default_factory=lambda: ConsolidationConfig(
            strategy=ConsolidationStrategy.AFTER_EVERY_SYNC,
            after_n_syncs=3,
            similarity_threshold=0.6,
            merge_threshold=0.85,
            min_facts_for_clustering=3,
            staleness_refresh_days=7,
        )
    )
    wiki: WikiConfig = Field(
        default_factory=lambda: WikiConfig(
            enabled=True,
            generation_strategy=WikiGenerationStrategy.AFTER_CONSOLIDATION,
            auto_regenerate_on_stale=True,
            min_facts_for_generation=5,
            topic_subpage_threshold=15,
        )
    )
    max_concurrent_syncs: int = 3
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))


class ResolvedPolicy(BaseModel):
    """Fully-resolved policy with no null fields — ready for use by pipeline."""

    sync: SyncConfig
    ingestion: IngestionConfig
    consolidation: ConsolidationConfig
    wiki: WikiConfig
