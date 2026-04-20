"""Development-only endpoints. NOT for production use."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from beever_atlas.infra.auth import require_admin

router = APIRouter(
    prefix="/api/dev",
    tags=["dev"],
    dependencies=[Depends(require_admin)],
)
logger = logging.getLogger(__name__)


@router.post("/reset")
async def reset_all_data() -> dict:
    """Wipe all synced data, connections, policies, and checkpoints.

    Resets the application to a fresh state as if it was just installed.
    This is destructive and irreversible — development use only.
    """
    from beever_atlas.stores import get_stores

    stores = get_stores()
    db = stores.mongodb.db
    results: dict[str, object] = {}
    channels_with_state: list[str] = []

    # 1. Clear Weaviate (all facts, clusters, summaries)
    try:
        # Get all channels that have data, then delete each
        channels_with_state = []
        async for doc in db["channel_sync_state"].find({}, {"channel_id": 1}):
            channels_with_state.append(doc["channel_id"])

        total_deleted = 0
        for cid in channels_with_state:
            try:
                n = await stores.weaviate.delete_by_channel(cid)
                total_deleted += n
            except Exception:
                pass

        # Catch-all: delete any orphaned objects not tied to a tracked channel
        try:
            orphaned = await stores.weaviate.delete_all()
            total_deleted += orphaned
        except Exception:
            pass

        results["weaviate"] = (
            f"deleted {total_deleted} objects across {len(channels_with_state)} channels"
        )
    except Exception as exc:
        results["weaviate_error"] = str(exc)

    # 2. Clear ALL graph data (Neo4j/Nebula — full wipe)
    try:
        # Try direct driver access for full wipe (Neo4j)
        driver = getattr(stores.graph, "_driver", None)
        if driver:
            async with driver.session() as neo_session:
                result = await neo_session.run(
                    "MATCH (n) DETACH DELETE n RETURN count(n) AS deleted"
                )
                record = await result.single()
                deleted = int(record["deleted"]) if record else 0
                results["graph"] = f"deleted {deleted} nodes (full wipe)"
        else:
            # Nebula or NullGraph — try channel-by-channel
            for cid in channels_with_state:
                try:
                    await stores.graph.delete_channel_data(cid)
                except Exception:
                    pass
            results["graph"] = "cleared (channel-by-channel)"
    except Exception as exc:
        results["graph_error"] = str(exc)

    # 3. Clear all MongoDB collections
    try:
        await db["sync_jobs"].delete_many({})
        await db["channel_sync_state"].delete_many({})
        await db["activity_events"].delete_many({})
        await db["write_intents"].delete_many({})
        await db["channel_policies"].delete_many({})
        await db["pipeline_checkpoints"].delete_many({})
        await db["platform_connections"].delete_many({})

        # Re-seed global policy defaults from Settings
        await db["global_policy_defaults"].delete_many({})
        # Trigger re-seed on next startup() call; for now insert from Settings
        from beever_atlas.infra.config import get_settings
        from beever_atlas.models.sync_policy import (
            ConsolidationConfig,
            ConsolidationStrategy,
            GlobalPolicyDefaults,
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
        await db["global_policy_defaults"].insert_one(defaults.model_dump(mode="json"))

        results["mongodb"] = "all collections cleared, defaults re-seeded"
    except Exception as exc:
        results["mongodb_error"] = str(exc)

    # 4. Invalidate caches
    try:
        from beever_atlas.services.policy_resolver import invalidate_defaults_cache

        invalidate_defaults_cache()
    except Exception:
        pass

    logger.warning("DEV RESET: all data wiped — %s", results)
    return {"status": "reset_complete", "details": results}
