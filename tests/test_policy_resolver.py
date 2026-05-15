"""Unit tests for policy resolution cascade."""

from beever_atlas.models.sync_policy import (
    ChannelPolicy,
    ConsolidationConfig,
    ConsolidationStrategy,
    GlobalPolicyDefaults,
    IngestionConfig,
    SyncConfig,
    SyncTriggerMode,
    WikiConfig,
)
from beever_atlas.services.policy_resolver import resolve_policy


def _defaults() -> GlobalPolicyDefaults:
    return GlobalPolicyDefaults()


def test_no_channel_policy_returns_global_defaults():
    defaults = _defaults()
    resolved = resolve_policy(None, defaults)

    assert resolved.sync.trigger_mode == SyncTriggerMode.MANUAL
    assert resolved.ingestion.batch_size == 10
    assert resolved.ingestion.quality_threshold == 0.5
    assert resolved.consolidation.strategy == ConsolidationStrategy.AFTER_EVERY_SYNC


def test_all_channel_fields_override_defaults():
    defaults = _defaults()
    channel = ChannelPolicy(
        channel_id="C123",
        sync=SyncConfig(
            trigger_mode=SyncTriggerMode.INTERVAL,
            interval_minutes=15,
            sync_type="full",
            max_messages=500,
            min_sync_interval_minutes=10,
        ),
        ingestion=IngestionConfig(
            batch_size=50,
            quality_threshold=0.8,
            max_facts_per_message=5,
            skip_entity_extraction=True,
            skip_graph_writes=True,
        ),
        consolidation=ConsolidationConfig(
            strategy=ConsolidationStrategy.AFTER_N_SYNCS,
            after_n_syncs=5,
            similarity_threshold=0.7,
            merge_threshold=0.9,
            min_facts_for_clustering=20,
            staleness_refresh_days=14,
        ),
    )
    resolved = resolve_policy(channel, defaults)

    assert resolved.sync.trigger_mode == SyncTriggerMode.INTERVAL
    assert resolved.sync.interval_minutes == 15
    assert resolved.sync.sync_type == "full"
    assert resolved.sync.max_messages == 500
    assert resolved.ingestion.batch_size == 50
    assert resolved.ingestion.quality_threshold == 0.8
    assert resolved.ingestion.skip_entity_extraction is True
    assert resolved.consolidation.strategy == ConsolidationStrategy.AFTER_N_SYNCS
    assert resolved.consolidation.after_n_syncs == 5
    assert resolved.consolidation.similarity_threshold == 0.7


def test_mixed_null_and_nonnull_fields():
    defaults = _defaults()
    channel = ChannelPolicy(
        channel_id="C456",
        sync=SyncConfig(
            trigger_mode=SyncTriggerMode.CRON,
            cron_expression="0 9 * * *",
            # sync_type, max_messages, min_sync_interval_minutes are None -> inherit
        ),
        ingestion=IngestionConfig(
            batch_size=50,
            # quality_threshold is None -> inherit 0.5
            skip_entity_extraction=True,
            # skip_graph_writes is None -> inherit False
        ),
        consolidation=ConsolidationConfig(
            strategy=ConsolidationStrategy.MANUAL,
            # all thresholds None -> inherit
        ),
    )
    resolved = resolve_policy(channel, defaults)

    # Channel values
    assert resolved.sync.trigger_mode == SyncTriggerMode.CRON
    assert resolved.sync.cron_expression == "0 9 * * *"
    assert resolved.ingestion.batch_size == 50
    assert resolved.ingestion.skip_entity_extraction is True
    assert resolved.consolidation.strategy == ConsolidationStrategy.MANUAL

    # Global defaults inherited
    assert resolved.sync.sync_type == "auto"
    assert resolved.sync.max_messages == 1000
    assert resolved.sync.min_sync_interval_minutes == 5
    assert resolved.ingestion.quality_threshold == 0.5
    assert resolved.ingestion.skip_graph_writes is False
    assert resolved.consolidation.similarity_threshold == 0.6
    assert resolved.consolidation.merge_threshold == 0.85
    assert resolved.consolidation.min_facts_for_clustering == 3


# ---------------------------------------------------------------------------
# WikiConfig.maintenance_mode resolution (per-channel override)
# ---------------------------------------------------------------------------


def test_per_channel_auto_overrides_global_manual():
    """Channel ``wiki.maintenance_mode="auto"`` wins over a global ``"manual"``
    default. The lifespan closure consults this resolved value at fire time
    so an operator's UI toggle takes effect on the next extraction batch.
    """
    defaults = GlobalPolicyDefaults()
    # Force the global wiki default to manual (the env-level fallback comes
    # from app settings, not the policy doc; here we simulate the global
    # default carrying ``manual`` for completeness).
    defaults.wiki = WikiConfig(maintenance_mode="manual")

    channel = ChannelPolicy(
        channel_id="C1",
        wiki=WikiConfig(maintenance_mode="auto"),
    )
    resolved = resolve_policy(channel, defaults)
    assert resolved.wiki.maintenance_mode == "auto"


def test_per_channel_inherit_falls_through_to_global_default():
    """Per-channel ``maintenance_mode=None`` (the wire-level "inherit" value)
    yields the global default — letting the lifespan check the env var when
    the global is also None.
    """
    defaults = GlobalPolicyDefaults()
    defaults.wiki = WikiConfig(maintenance_mode="auto")

    channel = ChannelPolicy(
        channel_id="C1",
        wiki=WikiConfig(maintenance_mode=None),
    )
    resolved = resolve_policy(channel, defaults)
    assert resolved.wiki.maintenance_mode == "auto"


def test_no_channel_policy_inherits_global_maintenance_mode():
    """No channel policy at all → resolver returns global defaults verbatim."""
    defaults = GlobalPolicyDefaults()
    defaults.wiki = WikiConfig(maintenance_mode="auto")
    resolved = resolve_policy(None, defaults)
    assert resolved.wiki.maintenance_mode == "auto"


def test_both_none_means_no_per_policy_value():
    """When channel + default both leave ``maintenance_mode=None``, the
    resolved value is None — the lifespan then consults the env var.
    """
    defaults = GlobalPolicyDefaults()
    # ``WikiConfig`` field default is None
    channel = ChannelPolicy(channel_id="C1", wiki=WikiConfig())
    resolved = resolve_policy(channel, defaults)
    assert resolved.wiki.maintenance_mode is None
