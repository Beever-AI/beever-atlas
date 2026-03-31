"""Stage 7: PersisterAgent — write embedded facts and validated entities to all stores.

Reads:
  - ``session.state["embedded_facts"]``     (from EmbedderAgent)
  - ``session.state["validated_entities"]`` (from CrossBatchValidatorAgent)

Writes:
  - ``session.state["persist_result"]``

Implemented as a ``BaseAgent`` subclass (no LLM calls). Uses the outbox pattern:
a ``WriteIntent`` is created in MongoDB first, then Weaviate and Neo4j are
written, and the intent is marked complete. The ``WriteReconciler`` handles
any writes that fail before completion.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from beever_atlas.stores import get_stores
from beever_atlas.models import AtomicFact, GraphEntity, GraphRelationship

logger = logging.getLogger(__name__)


class PersisterAgent(BaseAgent):
    """Persists embedded facts and validated entities to Weaviate and Neo4j.

    Uses the outbox (``WriteIntent``) pattern for durability: writes are
    recorded in MongoDB before being dispatched to the vector and graph stores.
    """

    model_config = {"arbitrary_types_allowed": True}

    async def _run_async_impl(
        self,
        ctx: InvocationContext,
    ) -> AsyncGenerator[Event, None]:
        """Execute the full persistence sequence and write ``persist_result``."""
        sync_job_id = ctx.session.state.get("sync_job_id", "unknown")
        channel_id = ctx.session.state.get("channel_id", "unknown")
        batch_num = ctx.session.state.get("batch_num", "?")
        embedded_facts: list[dict[str, Any]] = (
            ctx.session.state.get("embedded_facts") or []
        )
        validated_payload: dict[str, Any] = (
            ctx.session.state.get("validated_entities") or {}
        )
        entity_dicts: list[dict[str, Any]] = validated_payload.get("entities") or []
        relationship_dicts: list[dict[str, Any]] = (
            validated_payload.get("relationships") or []
        )

        stores = get_stores()

        # --- 1. Create outbox write intent in MongoDB ---
        intent_id = await stores.mongodb.create_write_intent(
            facts=embedded_facts,
            entities=entity_dicts,
            relationships=relationship_dicts,
        )
        logger.info(
            "PersisterAgent: intent created job_id=%s channel=%s batch=%s intent=%s facts=%d entities=%d relationships=%d",
            sync_job_id,
            channel_id,
            batch_num,
            intent_id,
            len(embedded_facts),
            len(entity_dicts),
            len(relationship_dicts),
        )

        # --- 2. Build media lookup from preprocessed messages ---
        # The preprocessor sets source_media_urls/source_media_type on enriched messages.
        # The LLM doesn't pass these through, so we join by source_message_id.
        preprocessed_messages: list[dict[str, Any]] = (
            ctx.session.state.get("preprocessed_messages") or []
        )
        media_lookup: dict[str, dict[str, Any]] = {}
        for pm in preprocessed_messages:
            for key in ("ts", "message_id", "source_message_id"):
                val = pm.get(key)
                if val and val not in media_lookup:
                    media_lookup[val] = pm

        # --- 3. Convert dicts to Pydantic models ---
        facts: list[AtomicFact] = []
        for idx, fd in enumerate(embedded_facts):
            platform = fd.get("platform", "slack")
            # Use session channel_id — the LLM output doesn't include it.
            fact_channel_id = fd.get("channel_id") or channel_id
            message_ts = fd.get("message_ts", "")
            fact_id = AtomicFact.deterministic_id(platform, fact_channel_id, message_ts, idx)
            fact_data = {k: v for k, v in fd.items() if k != "id"}
            fact_data["channel_id"] = fact_channel_id

            # Join media provenance from preprocessed message
            source_msg = (
                media_lookup.get(fd.get("source_message_id", ""))
                or media_lookup.get(fd.get("message_ts", ""))
            )
            if source_msg:
                media_urls = source_msg.get("source_media_urls") or []
                fact_data["source_media_url"] = media_urls[0] if media_urls else ""
                fact_data["source_media_type"] = source_msg.get("source_media_type", "")

            fact = AtomicFact(id=fact_id, **fact_data)
            facts.append(fact)

        entities: list[GraphEntity] = []
        for ed in entity_dicts:
            cleaned = {k: v for k, v in ed.items() if k != "id"}
            raw_props = cleaned.get("properties")
            if isinstance(raw_props, dict):
                cleaned["properties"] = {
                    k: v for k, v in raw_props.items() if v not in (None, "")
                }
            entity = GraphEntity(**cleaned)
            entities.append(entity)

        relationships: list[GraphRelationship] = []
        for rd in relationship_dicts:
            rel = GraphRelationship(**{k: v for k, v in rd.items() if k != "id"})
            relationships.append(rel)

        persist_errors: list[str] = []

        # --- 3. Batch upsert facts to Weaviate ---
        weaviate_ids: list[str] = []
        if facts:
            try:
                weaviate_ids = await stores.weaviate.batch_upsert_facts(facts)
                logger.info(
                    "PersisterAgent: weaviate upsert job_id=%s channel=%s batch=%s facts=%d",
                    sync_job_id,
                    channel_id,
                    batch_num,
                    len(weaviate_ids),
                )
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "PersisterAgent: weaviate failed job_id=%s channel=%s batch=%s: %s",
                    sync_job_id,
                    channel_id,
                    batch_num,
                    exc,
                )
                persist_errors.append(f"weaviate: {exc}")
        try:
            await stores.mongodb.mark_intent_weaviate_done(intent_id)
        except Exception:  # noqa: BLE001
            pass  # reconciler handles this

        # --- 4. Batch upsert entities and relationships to Neo4j ---
        try:
            if entities:
                await stores.neo4j.batch_upsert_entities(entities)
                logger.info(
                    "PersisterAgent: neo4j entity upsert job_id=%s channel=%s batch=%s entities=%d",
                    sync_job_id,
                    channel_id,
                    batch_num,
                    len(entities),
                )
            if relationships:
                await stores.neo4j.batch_upsert_relationships(relationships)
                logger.info(
                    "PersisterAgent: neo4j relationship upsert job_id=%s channel=%s batch=%s relationships=%d",
                    sync_job_id,
                    channel_id,
                    batch_num,
                    len(relationships),
                )
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "PersisterAgent: neo4j failed job_id=%s channel=%s batch=%s: %s",
                sync_job_id,
                channel_id,
                batch_num,
                exc,
            )
            persist_errors.append(f"neo4j: {exc}")
        try:
            await stores.mongodb.mark_intent_neo4j_done(intent_id)
        except Exception:  # noqa: BLE001
            pass  # reconciler handles this

        # --- 5. Create episodic links (entity → Event → weaviate_fact_id) ---
        for fact, weaviate_id in zip(facts, weaviate_ids, strict=True):
            for entity_name in fact.entity_tags:
                await stores.neo4j.create_episodic_link(
                    entity_name=entity_name,
                    weaviate_fact_id=weaviate_id,
                    message_ts=fact.message_ts,
                    channel_id=fact.channel_id,
                )

        # --- 6. Mark intent fully complete ---
        await stores.mongodb.mark_intent_complete(intent_id)
        logger.info(
            "PersisterAgent: intent complete job_id=%s channel=%s batch=%s intent=%s episodic_links_facts=%d",
            sync_job_id,
            channel_id,
            batch_num,
            intent_id,
            len(weaviate_ids),
        )

        # --- 7. Write result summary via event state_delta ---
        # ADK's InMemorySessionService only persists state changes that come
        # through event.actions.state_delta — direct ctx.session.state writes
        # modify a deep copy and are lost.
        persist_result = {
            "weaviate_ids": weaviate_ids,
            "entity_count": len(entities),
            "relationship_count": len(relationships),
            "errors": persist_errors,
        }

        yield Event(
            author=self.name,
            invocation_id=ctx.invocation_id,
            actions=EventActions(
                state_delta={"persist_result": persist_result},
            ),
        )
