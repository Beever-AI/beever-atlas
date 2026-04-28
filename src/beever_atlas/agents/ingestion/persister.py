"""Stage 6: PersisterAgent — write embedded facts and validated entities to all stores.

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

import asyncio
import logging
from typing import Any, AsyncGenerator

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions

from beever_atlas.stores import get_stores
from beever_atlas.models import AtomicFact, GraphEntity, GraphRelationship

logger = logging.getLogger(__name__)


# Shared between the Weaviate error-capture site and the
# `mark_intent_complete` conditional so a future format change at one site
# cannot silently break the other (Critic-flagged).
WEAVIATE_ERROR_PREFIX = "weaviate:"


def weaviate_failed(persist_errors: list[str]) -> bool:
    """Return True if `persist_errors` contains a Weaviate-prefixed entry.

    Used by `_run_async_impl` to gate `mark_intent_complete`: when Weaviate
    failed, the intent must stay in `weaviate_done=False` state so the
    `WriteReconciler` retries the Weaviate write automatically.
    """
    return any(err.startswith(WEAVIATE_ERROR_PREFIX) for err in persist_errors)


def match_media_by_word_overlap(
    fact: dict[str, Any],
    preprocessed_messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Attribute media/link provenance to a fact via a word-overlap heuristic.

    A single shared common word (e.g. "project", "demo") is not enough — that
    mis-attributes media across unrelated messages in long channels. Requires:
      - an author identity match between fact and candidate message, AND
      - either ≥3 unique overlapping ≥4-char words, OR
      - ≥1 overlapping word with len ≥6.

    Returns the matched preprocessed message dict, or ``None`` when no
    candidate satisfies the threshold. Callers should mark resulting facts
    with ``derived_from = "heuristic_word_overlap"``.
    """
    fact_author = (fact.get("author_id") or fact.get("author_name") or "").lower()
    if not fact_author:
        return None
    fact_text_lower = (fact.get("memory_text", "")).lower()
    fact_words = set(w for w in fact_text_lower.split() if len(w) > 3)
    best_match: dict[str, Any] | None = None
    best_score = 0
    best_long_overlap = False
    for pm in preprocessed_messages:
        pm_author = (pm.get("author_id") or pm.get("author_name") or "").lower()
        if not pm_author or pm_author != fact_author:
            continue
        pm_text = (pm.get("text", "")).lower()
        pm_words = set(w for w in pm_text.split() if len(w) > 3)
        shared = pm_words & fact_words
        overlap = len(shared)
        has_long = any(len(w) >= 6 for w in shared)
        qualifies = overlap >= 3 or (has_long and overlap >= 1)
        if qualifies and overlap > best_score:
            best_score = overlap
            best_long_overlap = has_long
            best_match = pm
    if best_match and (best_score >= 3 or best_long_overlap):
        return best_match
    return None


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
        from beever_atlas.agents.callbacks.checkpoint_skip import should_skip_stage

        if should_skip_stage(ctx.session.state, "persist_result", self.name):
            yield Event(author=self.name, invocation_id=ctx.invocation_id)
            return

        sync_job_id = ctx.session.state.get("sync_job_id", "unknown")
        channel_id = ctx.session.state.get("channel_id", "unknown")
        batch_num = ctx.session.state.get("batch_num", "?")
        # Source language for this batch (BCP-47). Seeded by BatchProcessor.
        # Defaults to "en" when language detection is off or unavailable so
        # existing channels keep their historical behavior.
        batch_source_lang: str = ctx.session.state.get("source_language") or "en"
        embedded_facts: list[dict[str, Any]] = ctx.session.state.get("embedded_facts") or []
        raw_validated = ctx.session.state.get("validated_entities")
        if not isinstance(raw_validated, dict):
            if raw_validated:
                logger.warning(
                    "PersisterAgent: validated_entities is %s, not dict; treating as empty batch=%s",
                    type(raw_validated).__name__,
                    batch_num,
                )
            validated_payload: dict[str, Any] = {}
        else:
            validated_payload = raw_validated
        entity_dicts: list[dict[str, Any]] = [
            e for e in (validated_payload.get("entities") or []) if isinstance(e, dict)
        ]
        relationship_dicts: list[dict[str, Any]] = [
            r for r in (validated_payload.get("relationships") or []) if isinstance(r, dict)
        ]

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
            for key in ("msg_id", "ts", "message_id", "source_message_id"):
                val = pm.get(key)
                if val and val not in media_lookup:
                    media_lookup[val] = pm

        logger.info(
            "PersisterAgent: media lookup keys=%s, mixed_msgs=%d",
            list(media_lookup.keys())[:10],
            sum(1 for pm in preprocessed_messages if pm.get("source_media_urls")),
        )

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
            src_msg_id = fd.get("source_message_id", "")
            src_msg_ts = fd.get("message_ts", "")
            source_msg = media_lookup.get(src_msg_id) or media_lookup.get(src_msg_ts)
            # Fallback: content-based matching when LLM doesn't output source_message_id
            if not source_msg:
                fact_text_lower = (fd.get("memory_text", "")).lower()

                # Pass 1: URL matching — if the fact text contains a URL from a message
                for pm in preprocessed_messages:
                    for url in pm.get("source_link_urls") or []:
                        if url.lower() in fact_text_lower:
                            source_msg = pm
                            break
                    if source_msg:
                        break

            heuristic_match = False
            if not source_msg:
                matched = match_media_by_word_overlap(fd, preprocessed_messages)
                if matched is not None:
                    source_msg = matched
                    heuristic_match = True

            if source_msg:
                media_urls = source_msg.get("source_media_urls") or []
                if media_urls:
                    logger.info(
                        "PersisterAgent: fact[%d] matched media_urls=%d media_type=%s",
                        idx,
                        len(media_urls),
                        source_msg.get("source_media_type", ""),
                    )
                fact_data["source_media_urls"] = media_urls
                fact_data["source_media_url"] = media_urls[0] if media_urls else ""
                fact_data["source_media_type"] = source_msg.get("source_media_type", "")
                fact_data["source_media_names"] = source_msg.get("source_media_names") or []
                # Thread link metadata
                fact_data["source_link_urls"] = source_msg.get("source_link_urls") or []
                fact_data["source_link_titles"] = source_msg.get("source_link_titles") or []
                fact_data["source_link_descriptions"] = (
                    source_msg.get("source_link_descriptions") or []
                )
                if heuristic_match:
                    fact_data["derived_from"] = "heuristic_word_overlap"

            # Normalize fact_type from LLM output
            raw_type = str(fact_data.get("fact_type", "")).lower().strip()
            valid_types = {"decision", "opinion", "observation", "action_item", "question"}
            fact_data["fact_type"] = (
                raw_type if raw_type in valid_types else "observation" if raw_type else ""
            )
            # Pass through thread_context_summary as-is (already a string)

            # Tag the fact with the batch's source language so wiki/QA can
            # translate on-demand while preserving native memory. The LLM may
            # have already populated this from the schema; keep its value if
            # present, else fall back to the batch-level tag.
            if not fact_data.get("source_lang"):
                fact_data["source_lang"] = batch_source_lang

            fact = AtomicFact(id=fact_id, **fact_data)
            facts.append(fact)

        entities: list[GraphEntity] = []
        for ed in entity_dicts:
            cleaned = {k: v for k, v in ed.items() if k != "id"}
            raw_props = cleaned.get("properties")
            if isinstance(raw_props, dict):
                cleaned["properties"] = {k: v for k, v in raw_props.items() if v not in (None, "")}
            # Tag the entity with the batch's source language (entity registry
            # will preserve this across dedup merges).
            if not cleaned.get("source_lang"):
                cleaned["source_lang"] = batch_source_lang
            entity = GraphEntity(**cleaned)
            entities.append(entity)

        # --- Batch compute name_vector for all entities ---
        try:
            entity_names = [e.name for e in entities if e.name]
            if entity_names:
                name_vectors = await stores.entity_registry.compute_name_embeddings_batch(
                    entity_names
                )
                for entity in entities:
                    if entity.name in name_vectors:
                        entity.name_vector = name_vectors[entity.name]
        except Exception:  # noqa: BLE001
            logger.warning(
                "PersisterAgent: name_vector batch computation failed job_id=%s, continuing without vectors",
                sync_job_id,
                exc_info=True,
            )

        relationships: list[GraphRelationship] = []
        for rd in relationship_dicts:
            rel = GraphRelationship(**{k: v for k, v in rd.items() if k != "id"})
            relationships.append(rel)

        persist_errors: list[str] = []
        skip_graph = ctx.session.state.get("skip_graph_writes", False)

        # --- 3. Parallel upsert to Weaviate and Neo4j ---
        async def _upsert_weaviate() -> list[str]:
            if not facts:
                return []
            ids = await stores.weaviate.batch_upsert_facts(facts)
            logger.info(
                "PersisterAgent: weaviate upsert job_id=%s channel=%s batch=%s facts=%d",
                sync_job_id,
                channel_id,
                batch_num,
                len(ids),
            )
            await stores.mongodb.mark_intent_weaviate_done(intent_id)
            return ids

        async def _upsert_graph() -> None:
            if skip_graph:
                logger.info(
                    "PersisterAgent: skip_graph_writes=true, skipping graph upserts job_id=%s channel=%s batch=%s",
                    sync_job_id,
                    channel_id,
                    batch_num,
                )
            else:
                if entities:
                    await stores.graph.batch_upsert_entities(entities)
                    logger.info(
                        "PersisterAgent: neo4j entity upsert job_id=%s channel=%s batch=%s entities=%d",
                        sync_job_id,
                        channel_id,
                        batch_num,
                        len(entities),
                    )
                if relationships:
                    await stores.graph.batch_upsert_relationships(relationships)
                    logger.info(
                        "PersisterAgent: neo4j relationship upsert job_id=%s channel=%s batch=%s relationships=%d",
                        sync_job_id,
                        channel_id,
                        batch_num,
                        len(relationships),
                    )
                # Store name_vectors on Neo4j entity nodes
                for entity in entities:
                    if entity.name_vector:
                        try:
                            await stores.entity_registry.store_name_vector(
                                entity.name, entity.name_vector
                            )
                        except Exception:  # noqa: BLE001
                            logger.warning(
                                "PersisterAgent: store_name_vector failed for %s",
                                entity.name,
                                exc_info=True,
                            )
            await stores.mongodb.mark_intent_neo4j_done(intent_id)

        weaviate_ids: list[str] = []
        results = await asyncio.gather(
            _upsert_weaviate(),
            _upsert_graph(),
            return_exceptions=True,
        )

        # Handle Weaviate result
        if isinstance(results[0], BaseException):
            logger.error(
                "PersisterAgent: weaviate failed job_id=%s channel=%s batch=%s: %s",
                sync_job_id,
                channel_id,
                batch_num,
                results[0],
            )
            persist_errors.append(f"{WEAVIATE_ERROR_PREFIX} {results[0]}")
        else:
            weaviate_ids = results[0]

        # Handle Neo4j result
        if isinstance(results[1], BaseException):
            logger.error(
                "PersisterAgent: neo4j failed job_id=%s channel=%s batch=%s: %s",
                sync_job_id,
                channel_id,
                batch_num,
                results[1],
            )
            persist_errors.append(f"neo4j: {results[1]}")

        # --- 4. Reconcile entity_tags: create stub entities for missing names ---
        # Only create stubs for names that appear in relationships to avoid orphan nodes.
        if not skip_graph:
            rel_names: set[str] = set()
            for rel in relationships:
                rel_names.add(rel.source)
                rel_names.add(rel.target)
            all_tag_names: set[str] = set()
            for f in facts:
                all_tag_names.update(f.entity_tags)
            # Intersect with relationship names — stubs without relationships are orphans
            all_tag_names &= rel_names
            extracted_names: set[str] = {e.name for e in entities}
            missing_names = all_tag_names - extracted_names
            if missing_names:
                try:
                    existing_names = await stores.graph.batch_find_entities_by_name(
                        list(missing_names)
                    )
                    missing_names -= existing_names
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "PersisterAgent: batch_find_entities_by_name failed", exc_info=True
                    )
                # Create stub entities for truly missing names
                stubs: list[GraphEntity] = []
                for name in missing_names:
                    stubs.append(
                        GraphEntity(
                            name=name,
                            type="Project",
                            scope="global",
                            properties={"stub": True},
                            source_message_id=facts[0].source_message_id if facts else "",
                            message_ts=facts[0].message_ts if facts else "",
                        )
                    )
                if stubs:
                    try:
                        await stores.graph.batch_upsert_entities(stubs)
                    except Exception:  # noqa: BLE001
                        logger.warning("PersisterAgent: stub entity creation failed", exc_info=True)
                if missing_names:
                    logger.info(
                        "PersisterAgent: created %d stub entities job_id=%s channel=%s batch=%s names=%s",
                        len(missing_names),
                        sync_job_id,
                        channel_id,
                        batch_num,
                        list(missing_names)[:5],
                    )

        # --- 5. Batch promote pending entities that now have relationships ---
        if not skip_graph and relationships:
            rel_entity_names: set[str] = set()
            for rel in relationships:
                rel_entity_names.add(rel.source)
                rel_entity_names.add(rel.target)
            try:
                promoted = await stores.graph.batch_promote_pending(list(rel_entity_names))
                if promoted:
                    logger.info("PersisterAgent: promoted %d pending entities", promoted)
            except Exception:  # noqa: BLE001
                logger.warning("PersisterAgent: batch_promote_pending failed", exc_info=True)

        # --- 6. Batch episodic links ---
        episodic_links: list[dict[str, Any]] = []
        if weaviate_ids:
            for fact, weaviate_id in zip(facts, weaviate_ids, strict=True):
                if skip_graph:
                    break
                for entity_name in fact.entity_tags:
                    episodic_links.append(
                        {
                            "entity_name": entity_name,
                            "weaviate_fact_id": weaviate_id,
                            "message_ts": fact.message_ts,
                            "channel_id": fact.channel_id,
                            "media_urls": fact.source_media_urls or [],
                            "link_urls": fact.source_link_urls or [],
                        }
                    )
        else:
            logger.warning(
                "PersisterAgent: skipping episodic-link cross-reference (weaviate_ids empty — Weaviate failed for %d facts) job_id=%s channel=%s batch=%s",
                len(facts),
                sync_job_id,
                channel_id,
                batch_num,
            )

        if episodic_links and not skip_graph:
            try:
                ep_count = await stores.graph.batch_create_episodic_links(episodic_links)
                logger.info(
                    "PersisterAgent: created %d episodic links (batch) job_id=%s channel=%s batch=%s",
                    ep_count,
                    sync_job_id,
                    channel_id,
                    batch_num,
                )
            except Exception:  # noqa: BLE001
                logger.warning("PersisterAgent: batch episodic links failed", exc_info=True)

        # --- 7. Batch media nodes and entity-media links ---
        media_items: list[dict[str, Any]] = []
        entity_media_links: list[dict[str, Any]] = []
        if weaviate_ids:
            for fact, weaviate_id in zip(facts, weaviate_ids, strict=True):
                if skip_graph:
                    break
                all_media_urls = [
                    (url, fact.source_media_type or "file")
                    for url in (fact.source_media_urls or [])
                ] + [(url, "link") for url in (fact.source_link_urls or [])]
                for url, mtype in all_media_urls:
                    title = ""
                    if mtype == "link":
                        idx = (
                            (fact.source_link_urls or []).index(url)
                            if url in (fact.source_link_urls or [])
                            else -1
                        )
                        if idx >= 0 and idx < len(fact.source_link_titles or []):
                            title = fact.source_link_titles[idx]
                        if not title:
                            try:
                                parts = url.split("//")[-1].split("/")
                                title = "/".join(parts[:3]) if len(parts) > 2 else parts[0]
                            except Exception:
                                title = url
                    else:
                        media_urls_list = fact.source_media_urls or []
                        media_names = fact.source_media_names or []
                        if url in media_urls_list:
                            idx = media_urls_list.index(url)
                            if idx < len(media_names) and media_names[idx]:
                                title = media_names[idx]
                    media_items.append(
                        {
                            "url": url,
                            "media_type": mtype,
                            "title": title,
                            "channel_id": fact.channel_id,
                            "message_ts": fact.message_ts,
                        }
                    )
                    for entity_name in fact.entity_tags:
                        entity_media_links.append(
                            {
                                "entity_name": entity_name,
                                "media_url": url,
                            }
                        )
        else:
            logger.warning(
                "PersisterAgent: skipping media-node cross-reference (weaviate_ids empty — Weaviate failed for %d facts) job_id=%s channel=%s batch=%s",
                len(facts),
                sync_job_id,
                channel_id,
                batch_num,
            )

        if media_items and not skip_graph:
            try:
                await stores.graph.batch_upsert_media(media_items)
            except Exception:  # noqa: BLE001
                logger.warning("PersisterAgent: batch_upsert_media failed", exc_info=True)
        if entity_media_links and not skip_graph:
            try:
                await stores.graph.batch_link_entities_to_media(entity_media_links)
            except Exception:  # noqa: BLE001
                logger.warning("PersisterAgent: batch_link_entities_to_media failed", exc_info=True)

        # --- 6. Mark intent fully complete (skip if Weaviate failed; reconciler retries) ---
        if not weaviate_failed(persist_errors):
            await stores.mongodb.mark_intent_complete(intent_id)
            logger.info(
                "PersisterAgent: intent complete job_id=%s channel=%s batch=%s intent=%s episodic_links_facts=%d",
                sync_job_id,
                channel_id,
                batch_num,
                intent_id,
                len(weaviate_ids),
            )
        else:
            logger.warning(
                "PersisterAgent: leaving intent %s pending for reconciler retry (Weaviate failed) job_id=%s channel=%s batch=%s",
                intent_id,
                sync_job_id,
                channel_id,
                batch_num,
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
