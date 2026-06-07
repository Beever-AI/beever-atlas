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
from beever_atlas.infra.config import get_settings

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


def native_message_id(source_msg: dict[str, Any]) -> str:
    """The platform-native message id of a preprocessed source message.

    This is the value (e.g. the numeric Slack ts ``1779390885.369099``) needed to
    build a real message permalink — as opposed to the synthetic ``msg-N``
    reference the LLM emits. Prefers the explicit ``native_message_id`` the
    preprocessor stamps, falling back to the raw ``message_id``. Returns ``""``
    when neither is present (citation simply stays unlinked).
    """
    return source_msg.get("native_message_id") or source_msg.get("message_id") or ""


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
                    "PersisterAgent: validated_entities is %s, not dict; falling back to extracted_entities batch=%s",
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
        # If the cross_batch_validator's output was lost (truncated JSON
        # that could not be recovered by adk_recovery), fall back to the
        # entity_extractor's raw output rather than persisting an empty
        # set. The upstream extractor data has already been schema-
        # validated; the only thing we lose by skipping the validator is
        # the cross-batch dedup pass — preferable to silently dropping
        # ~100 entities + relationships per affected batch. Observed in
        # production logs (batch 21, 23) where validated_entities became
        # a "failed_recoverable=True" sentinel string and persister wrote
        # facts=N entities=0 relationships=0.
        if not entity_dicts and not relationship_dicts:
            raw_extracted = ctx.session.state.get("extracted_entities")
            if isinstance(raw_extracted, dict):
                fallback_ents = [
                    e for e in (raw_extracted.get("entities") or []) if isinstance(e, dict)
                ]
                fallback_rels = [
                    r for r in (raw_extracted.get("relationships") or []) if isinstance(r, dict)
                ]
                if fallback_ents or fallback_rels:
                    logger.warning(
                        "PersisterAgent: using extracted_entities fallback for batch=%s "
                        "(recovered %d entities + %d relationships)",
                        batch_num,
                        len(fallback_ents),
                        len(fallback_rels),
                    )
                    entity_dicts = fallback_ents
                    relationship_dicts = fallback_rels

        stores = get_stores()

        # --- 1. Create outbox write intent in MongoDB ---
        # Persist the owning channel top-level so the channel hard-purge
        # (delete-channel-v2) can drop this intent in one indexed pass.
        # Extraction batches are per-channel (session-scoped), so the
        # session ``channel_id`` is the right value. The ``"unknown"``
        # sentinel (channel_id absent from session state) maps to None so
        # the purge filter never matches a bogus channel.
        intent_channel_id = channel_id if channel_id and channel_id != "unknown" else None
        intent_id = await stores.mongodb.create_write_intent(
            facts=embedded_facts,
            entities=entity_dicts,
            relationships=relationship_dicts,
            channel_id=intent_channel_id,
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
            # Use session channel_id — the LLM output doesn't include it.
            fact_channel_id = fd.get("channel_id") or channel_id
            # Content-derived deterministic ID. The previous
            # ``(platform, channel_id, message_ts, idx)`` key shifted whenever
            # the LLM produced facts in a different order on retry, creating
            # phantom Weaviate duplicates. ``idx`` is intentionally unused now.
            entity_names = fd.get("entity_tags") or []
            fact_id = AtomicFact.deterministic_id(fd.get("memory_text", ""), entity_names)
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
                # Preserve the platform-native message id (e.g. the numeric Slack
                # ts) for permalink construction. The LLM only emits the synthetic
                # ``msg-N`` reference, which can't build a real message URL — so
                # override source_message_id with the matched message's native id.
                native_id = native_message_id(source_msg)
                if native_id:
                    fact_data["source_message_id"] = native_id
                # Stamp platform + guild_id from the matched source message so
                # the permalink resolver can pick the right URL template and
                # (for Discord) build a clickable message link. Both fields
                # ride on the preprocessed message dict (copied verbatim from
                # the NormalizedMessage via ``vars()`` upstream).
                #
                # REGRESSION GUARD: only override ``platform`` when the source
                # actually carries one. Slack messages set platform="slack"
                # here and keep resolving via the Slack archives template; if
                # the source had no platform we leave fact_data's existing
                # value (AtomicFact's "slack" default) untouched so nothing
                # that worked before breaks.
                src_platform = source_msg.get("platform")
                if src_platform:
                    fact_data["platform"] = src_platform
                src_guild_id = source_msg.get("guild_id")
                if src_guild_id:
                    fact_data["guild_id"] = src_guild_id
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
                    # Verb normalization (PR-B): collapse the long tail of
                    # LLM-emitted verbs into a small canonical set BEFORE
                    # writing to Neo4j. Mutates rel.type in place; source/
                    # target/direction untouched. Audit ledger is rolled
                    # into sync_summary.normalizations[] for replay-based
                    # recovery (per v2 plan §B.5 — no per-edge field).
                    from beever_atlas.agents.ingestion.verb_normalizer import (
                        normalize_relationships,
                        summarize_normalizations,
                    )

                    # Use a fresh name for the normalized list so the
                    # closure capture of the outer ``relationships`` (set
                    # at line 313 of the enclosing function) isn't shadowed
                    # by a function-local assignment — F823 from ruff.
                    normalized_rels, _norm_log = normalize_relationships(
                        relationships, sync_job_id=sync_job_id
                    )
                    if _norm_log and channel_id and sync_job_id:
                        from beever_atlas.services.batch_processor import (
                            increment_sync_metric,
                        )

                        increment_sync_metric(
                            channel_id,
                            sync_job_id,
                            "relationships_normalized_total",
                            sum(1 for _ in _norm_log),
                        )
                        # Persist per-rule ledger rows so an operator can
                        # replay the mapping later. Stored as a structured
                        # log line keyed by sync_job_id; sync_summary
                        # roll-up reads these via the sync_summary
                        # collector.
                        summary_rows = summarize_normalizations(_norm_log)
                        for row in summary_rows:
                            logger.info(
                                "sync_summary: metric=verb_normalization "
                                "original=%s canonical=%s rule=%s count=%s "
                                "channel_id=%s sync_job_id=%s",
                                row["original_verb"],
                                row["canonical"],
                                row["rule"],
                                row["count"],
                                channel_id,
                                sync_job_id,
                            )
                    _rel_eids = await stores.graph.batch_upsert_relationships(
                        normalized_rels,
                        channel_id=channel_id,
                        sync_job_id=sync_job_id,
                        batch_idx=batch_num,
                    )
                    _dropped = sum(1 for eid in _rel_eids if not eid)
                    logger.info(
                        "PersisterAgent: neo4j relationship upsert job_id=%s channel=%s batch=%s relationships=%d",
                        sync_job_id,
                        channel_id,
                        batch_num,
                        len(normalized_rels),
                    )
                    if _dropped and channel_id and sync_job_id:
                        from beever_atlas.services.batch_processor import increment_sync_metric

                        increment_sync_metric(
                            channel_id, sync_job_id, "relationships_dropped_total", _dropped
                        )
                # Store name_vectors on Neo4j entity nodes
                settings = get_settings()
                if settings.neo4j_batch_name_vector:
                    try:
                        items = [
                            (e.name, e.name_vector) for e in entities if e.name_vector is not None
                        ]
                        await stores.entity_registry.batch_store_name_vectors(items)
                        logger.info(
                            "PersisterAgent: batch_store_name_vectors items=%d batch=%s",
                            len(items),
                            batch_num,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning(
                            "PersisterAgent: batch_store_name_vectors failed, falling back to per-entity batch=%s",
                            batch_num,
                            exc_info=True,
                        )
                        for entity in entities:
                            if not entity.name_vector:
                                continue
                            try:
                                await stores.entity_registry.store_name_vector(
                                    entity.name, entity.name_vector
                                )
                            except Exception:  # noqa: BLE001
                                logger.exception("store_name_vector failed entity=%s", entity.name)
                else:
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
        # Heal-path tightening:
        #   * Confidence gate — only relationships with confidence ≥ 0.8 seed
        #     stubs. Lower-confidence rels are too speculative to manufacture
        #     entities from.
        #   * 2-unknown drop — when BOTH endpoints of a rel are absent from
        #     the typed-entity set, the rel cannot be healed and should not
        #     have been written. We increment ``relationships_dropped_total``
        #     with reason ``both_endpoints_unknown`` so observability picks
        #     it up. The rel has already been committed by the earlier
        #     ``batch_upsert_relationships`` call; the gate here prevents
        #     us from also manufacturing the two stub Entity rows that
        #     would otherwise connect.
        if not skip_graph:
            extracted_names: set[str] = {e.name for e in entities}
            high_conf_rels = [r for r in relationships if r.confidence >= 0.8]
            rel_names: set[str] = set()
            both_unknown_count = 0
            for rel in high_conf_rels:
                src_known = rel.source in extracted_names
                tgt_known = rel.target in extracted_names
                if not src_known and not tgt_known:
                    both_unknown_count += 1
                    continue
                rel_names.add(rel.source)
                rel_names.add(rel.target)
            if both_unknown_count and channel_id and sync_job_id:
                from beever_atlas.services.batch_processor import increment_sync_metric

                increment_sync_metric(
                    channel_id,
                    sync_job_id,
                    "relationships_dropped_total",
                    both_unknown_count,
                )
                logger.info(
                    "PersisterAgent: dropped %d relationships reason=both_endpoints_unknown job_id=%s channel=%s batch=%s",
                    both_unknown_count,
                    sync_job_id,
                    channel_id,
                    batch_num,
                )
            all_tag_names: set[str] = set()
            for f in facts:
                all_tag_names.update(f.entity_tags)
            # Intersect with relationship names — stubs without relationships are orphans
            all_tag_names &= rel_names
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
                # Create stub entities for truly missing names. They are
                # tagged ``type='Unresolved'`` (backend-only synthetic
                # type, never emitted by the LLM) plus ``awaiting_type``
                # so a later typed ``upsert_entity`` for the same name
                # heals the row in place via the heal-path in
                # ``Neo4jStore.upsert_entity``.
                stubs: list[GraphEntity] = []
                for name in missing_names:
                    stubs.append(
                        GraphEntity(
                            name=name,
                            type="Unresolved",
                            scope="global",
                            properties={
                                "stub": True,
                                "reason": "rel_endpoint",
                                "awaiting_type": True,
                            },
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
