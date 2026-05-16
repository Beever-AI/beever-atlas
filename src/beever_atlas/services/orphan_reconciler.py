"""Background reconciler for expired pending entities.

Periodically checks for pending entities that have exceeded their grace
period without gaining any relationships, and prunes them from Neo4j.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


async def prune_expired_orphans() -> int:
    """Prune pending entities that have exceeded the grace period.

    Also prunes Unresolved stub entities older than 24h that never
    gained any incident edges — see
    :meth:`Neo4jStore.prune_stub_orphans`. The total returned is the
    sum of both purges.
    """
    from beever_atlas.infra.config import get_settings
    from beever_atlas.stores import get_stores

    settings = get_settings()
    stores = get_stores()

    pending_count = 0
    stub_count = 0
    try:
        pending_count = await stores.graph.prune_expired_pending(
            grace_period_days=settings.orphan_grace_period_days,
        )
        if pending_count > 0:
            logger.info(
                "OrphanReconciler: pruned %d expired pending entities (grace=%d days)",
                pending_count,
                settings.orphan_grace_period_days,
            )
    except Exception:
        logger.warning("OrphanReconciler: prune_expired_pending failed", exc_info=True)

    try:
        stub_count = await stores.graph.prune_stub_orphans(ttl_hours=24)
        if stub_count > 0:
            logger.info(
                "OrphanReconciler: pruned %d Unresolved stub orphans (ttl=24h)",
                stub_count,
            )
    except Exception:
        logger.warning("OrphanReconciler: prune_stub_orphans failed", exc_info=True)

    return pending_count + stub_count
