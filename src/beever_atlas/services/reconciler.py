"""Background reconciler — retries failed outbox writes.

``WriteReconciler`` polls MongoDB for ``WriteIntent`` records that are not yet
fully complete (``weaviate_done`` or ``neo4j_done`` is False) and retries the
failed writes against Weaviate and/or Neo4j.

Usage (long-running background task)::

    reconciler = WriteReconciler()
    asyncio.create_task(reconciler.start_loop())
"""

from __future__ import annotations

import asyncio
import logging

from beever_atlas.infra.config import get_settings
from beever_atlas.stores import get_stores
from beever_atlas.models import AtomicFact, GraphEntity, GraphRelationship

logger = logging.getLogger(__name__)


class WriteReconciler:
    """Retries incomplete outbox writes from MongoDB.

    Scans for ``WriteIntent`` records that were created more than
    ``settings.reconciler_interval_minutes`` minutes ago but have not yet been
    marked fully complete, then replays the missing Weaviate or Neo4j writes.
    """

    def __init__(self) -> None:
        pass

    async def run_once(self) -> None:
        """Fetch pending intents and retry any incomplete store writes."""
        settings = get_settings()
        stores = get_stores()

        pending = await stores.mongodb.get_pending_intents(
            max_age_minutes=settings.reconciler_interval_minutes
        )

        if not pending:
            logger.debug("WriteReconciler: no pending intents found.")
            return

        logger.info("WriteReconciler: %d pending intent(s) to reconcile.", len(pending))

        for intent in pending:
            try:
                await self._reconcile_intent(intent.id, intent, stores)
            except Exception:
                logger.exception("WriteReconciler: failed to reconcile intent %s.", intent.id)

    async def _reconcile_intent(self, intent_id: str, intent: object, stores: object) -> None:  # type: ignore[override]
        """Retry the Weaviate and/or Neo4j writes for a single intent."""
        from beever_atlas.models import WriteIntent  # local import to avoid circularity

        wi: WriteIntent = intent  # type: ignore[assignment]

        # delete-channel-v2 Wave 0 — per-fact purge filter. An intent may carry
        # facts/entities for MULTIPLE channels (mixed-channel intents) and/or
        # have no top-level channel_id (pre-backfill rows). We therefore filter
        # at the per-item level on each row's own ``channel_id`` rather than
        # skipping the whole intent: drop facts/entities whose channel is being
        # purged, replay the rest, and STILL mark the intent complete at the end
        # so a purging channel cannot livelock the reconciler or resurrect data.
        # Best-effort: a Mongo blip fetching the set leaves us with an empty
        # set (replay everything) — the durable purge lock is the backstop.
        try:
            purging_ids = await stores.mongodb.get_purging_channel_ids()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — guard fetch must not break reconciliation
            logger.exception(
                "WriteReconciler: get_purging_channel_ids failed for intent %s — "
                "proceeding without purge filter",
                intent_id,
            )
            purging_ids = set()

        facts: list[AtomicFact] = []
        for fd in wi.facts:
            if fd.get("channel_id") in purging_ids:
                continue  # channel is being purged — do not resurrect this fact
            # Content-derived deterministic ID — same memory_text +
            # same sorted entity_tags yields the same UUID across retries.
            entity_names = fd.get("entity_tags") or []
            fact_id = AtomicFact.deterministic_id(fd.get("memory_text", ""), entity_names)
            fact = AtomicFact(id=fact_id, **{k: v for k, v in fd.items() if k != "id"})
            facts.append(fact)

        entities: list[GraphEntity] = [
            GraphEntity(**{k: v for k, v in ed.items() if k != "id"})
            for ed in wi.entities
            if ed.get("channel_id") not in purging_ids
        ]
        # Relationships pragmatically: GraphRelationship rows do not reliably
        # carry a channel_id on the intent payload, so we cannot per-row filter
        # them the way we do facts/entities. We drop ALL relationships when ANY
        # of the intent's facts/entities belonged to a purging channel (a
        # conservative over-drop) and otherwise replay them unchanged. This may
        # drop a relationship that bridged a surviving channel, but the
        # alternative (resurrecting edges into a purged channel's graph) is
        # worse; the Wave-2 graph delete + reaper converge the surviving side.
        intent_touches_purged_channel = any(
            fd.get("channel_id") in purging_ids for fd in wi.facts
        ) or any(ed.get("channel_id") in purging_ids for ed in wi.entities)
        relationships: list[GraphRelationship] = (
            []
            if (intent_touches_purged_channel and purging_ids)
            else [
                GraphRelationship(**{k: v for k, v in rd.items() if k != "id"})
                for rd in wi.relationships
            ]
        )

        if not wi.weaviate_done:
            if facts:
                await stores.weaviate.batch_upsert_facts(facts)  # type: ignore[attr-defined]
                logger.info(
                    "WriteReconciler: retried %d facts for intent %s.",
                    len(facts),
                    intent_id,
                )
            await stores.mongodb.mark_intent_weaviate_done(intent_id)  # type: ignore[attr-defined]

        if not wi.neo4j_done:
            if entities:
                await stores.graph.batch_upsert_entities(entities)  # type: ignore[attr-defined]
            if relationships:
                await stores.graph.batch_upsert_relationships(relationships)  # type: ignore[attr-defined]
            logger.info(
                "WriteReconciler: retried %d entities, %d relationships for intent %s.",
                len(entities),
                len(relationships),
                intent_id,
            )
            await stores.mongodb.mark_intent_neo4j_done(intent_id)  # type: ignore[attr-defined]

        await stores.mongodb.mark_intent_complete(intent_id)  # type: ignore[attr-defined]
        logger.info("WriteReconciler: intent %s reconciled.", intent_id)

    async def start_loop(self) -> None:
        """Run ``run_once`` indefinitely, sleeping between iterations.

        Errors inside a single run are logged but do not crash the loop.
        """
        settings = get_settings()
        interval = settings.reconciler_interval_minutes * 60

        logger.info("WriteReconciler: starting loop (interval=%ds).", interval)
        while True:
            try:
                await self.run_once()
            except Exception:
                logger.exception("WriteReconciler: unexpected error in reconciliation loop.")
            await asyncio.sleep(interval)
