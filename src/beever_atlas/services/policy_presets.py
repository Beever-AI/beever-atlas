"""Static policy preset definitions."""

from __future__ import annotations

from beever_atlas.models.sync_policy import (
    ConsolidationConfig,
    ConsolidationStrategy,
    IngestionConfig,
    SyncConfig,
    SyncTriggerMode,
    WikiConfig,
    WikiGenerationStrategy,
)

PRESETS: dict[str, dict] = {
    "real-time": {
        "name": "Real-time",
        "description": "Updates every 5 minutes with full extraction and auto-organization.",
        "sync": SyncConfig(
            trigger_mode=SyncTriggerMode.INTERVAL,
            interval_minutes=5,
            sync_type="auto",
            max_messages=1000,
            min_sync_interval_minutes=3,
        ),
        "ingestion": IngestionConfig(
            batch_size=10,
            quality_threshold=0.5,
            max_facts_per_message=2,
            skip_entity_extraction=False,
            skip_graph_writes=False,
        ),
        "consolidation": ConsolidationConfig(
            strategy=ConsolidationStrategy.AFTER_EVERY_SYNC,
            similarity_threshold=0.6,
            merge_threshold=0.85,
            min_facts_for_clustering=3,
        ),
        "wiki": WikiConfig(
            generation_strategy=WikiGenerationStrategy.AFTER_CONSOLIDATION,
            enabled=True,
            auto_regenerate_on_stale=True,
            min_facts_for_generation=5,
            topic_subpage_threshold=15,
        ),
    },
    "daily-digest": {
        "name": "Daily Digest",
        "description": "Syncs once a day at 2 AM with full extraction and auto-organization.",
        "sync": SyncConfig(
            trigger_mode=SyncTriggerMode.CRON,
            cron_expression="0 2 * * *",
            sync_type="auto",
            max_messages=5000,
            min_sync_interval_minutes=60,
        ),
        "ingestion": IngestionConfig(
            batch_size=20,
            quality_threshold=0.5,
            max_facts_per_message=3,
            skip_entity_extraction=False,
            skip_graph_writes=False,
        ),
        "consolidation": ConsolidationConfig(
            strategy=ConsolidationStrategy.AFTER_EVERY_SYNC,
            similarity_threshold=0.6,
            merge_threshold=0.85,
            min_facts_for_clustering=5,
        ),
        "wiki": WikiConfig(
            generation_strategy=WikiGenerationStrategy.AFTER_CONSOLIDATION,
            enabled=True,
            auto_regenerate_on_stale=True,
            min_facts_for_generation=10,
            topic_subpage_threshold=15,
        ),
    },
    "lightweight": {
        "name": "Lightweight",
        "description": "Syncs every hour with quick extraction. Organize knowledge manually.",
        "sync": SyncConfig(
            trigger_mode=SyncTriggerMode.INTERVAL,
            interval_minutes=60,
            sync_type="auto",
            max_messages=500,
            min_sync_interval_minutes=30,
        ),
        "ingestion": IngestionConfig(
            batch_size=15,
            quality_threshold=0.3,
            max_facts_per_message=2,
            skip_entity_extraction=True,
            skip_graph_writes=True,
        ),
        "consolidation": ConsolidationConfig(
            strategy=ConsolidationStrategy.MANUAL,
            similarity_threshold=0.5,
            merge_threshold=0.8,
            min_facts_for_clustering=10,
        ),
        "wiki": WikiConfig(
            generation_strategy=WikiGenerationStrategy.MANUAL,
            enabled=True,
            auto_regenerate_on_stale=False,
            min_facts_for_generation=10,
            topic_subpage_threshold=20,
        ),
    },
    "manual": {
        "name": "Manual",
        "description": "Full control — sync and organize only when you choose.",
        "sync": SyncConfig(
            trigger_mode=SyncTriggerMode.MANUAL,
            sync_type="auto",
            max_messages=1000,
            min_sync_interval_minutes=1,
        ),
        "ingestion": IngestionConfig(
            batch_size=10,
            quality_threshold=0.5,
            max_facts_per_message=2,
            skip_entity_extraction=False,
            skip_graph_writes=False,
        ),
        "consolidation": ConsolidationConfig(
            strategy=ConsolidationStrategy.MANUAL,
            similarity_threshold=0.6,
            merge_threshold=0.85,
            min_facts_for_clustering=3,
        ),
        "wiki": WikiConfig(
            generation_strategy=WikiGenerationStrategy.MANUAL,
            enabled=True,
            auto_regenerate_on_stale=False,
            min_facts_for_generation=5,
            topic_subpage_threshold=15,
        ),
    },
}


def get_preset_config(preset_name: str) -> dict | None:
    """Return a preset's config dict, or None if not found."""
    return PRESETS.get(preset_name)


def list_presets() -> list[dict]:
    """Return all presets with name, description, and config."""
    result = []
    for key, preset in PRESETS.items():
        result.append(
            {
                "id": key,
                "name": preset["name"],
                "description": preset["description"],
                "sync": preset["sync"].model_dump(mode="json"),
                "ingestion": preset["ingestion"].model_dump(mode="json"),
                "consolidation": preset["consolidation"].model_dump(mode="json"),
                "wiki": preset["wiki"].model_dump(mode="json"),
            }
        )
    return result
