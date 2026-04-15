"""Weaviate store client for AtomicFact storage and retrieval."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import uuid
from datetime import datetime
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.init import AdditionalConfig, Auth
from weaviate.classes.query import Filter
from weaviate.config import GrpcConfig

from beever_atlas.models import AtomicFact, MemoryFilters, PaginatedFacts

if TYPE_CHECKING:
    from beever_atlas.models.domain import ChannelSummary, EntityKnowledgeCard, TopicCluster

_GRPC_CHANNEL_OPTIONS: list[tuple[str, Any]] = [
    ("grpc.max_send_message_length", -1),
    ("grpc.max_receive_message_length", -1),
]
_ADDITIONAL_CONFIG = AdditionalConfig(grpc_config=GrpcConfig(channel_options=_GRPC_CHANNEL_OPTIONS))

COLLECTION_NAME = "MemoryFact"
logger = logging.getLogger(__name__)


class WeaviateStore:
    """Manages the MemoryFact collection in Weaviate for atomic fact storage."""

    def __init__(self, url: str, api_key: str = "") -> None:
        self._url = url
        self._api_key = api_key
        self._client: weaviate.WeaviateClient | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Connect to Weaviate and ensure schema exists."""

        def _connect() -> weaviate.WeaviateClient:
            from urllib.parse import urlparse

            parsed = urlparse(self._url)
            host = parsed.hostname or "localhost"
            port = parsed.port or (443 if parsed.scheme == "https" else 8080)
            secure = parsed.scheme == "https"

            auth = Auth.api_key(self._api_key) if self._api_key else None

            if host in ("localhost", "127.0.0.1") and not secure:
                return weaviate.connect_to_local(
                    port=port,
                    grpc_port=50051,
                    auth_credentials=auth,
                    additional_config=_ADDITIONAL_CONFIG,
                )

            return weaviate.connect_to_custom(
                http_host=host,
                http_port=port,
                http_secure=secure,
                grpc_host=host,
                grpc_port=50051,
                grpc_secure=secure,
                auth_credentials=auth,
                additional_config=_ADDITIONAL_CONFIG,
            )

        self._client = await asyncio.to_thread(_connect)
        await self.ensure_schema()

    async def shutdown(self) -> None:
        """Close the Weaviate client connection."""
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None

    # All expected properties for the MemoryFact collection.
    _EXPECTED_PROPERTIES: list[tuple[str, DataType]] = [
        ("memory_text", DataType.TEXT),
        ("quality_score", DataType.NUMBER),
        ("tier", DataType.TEXT),
        ("cluster_id", DataType.TEXT),
        ("channel_id", DataType.TEXT),
        ("platform", DataType.TEXT),
        ("author_id", DataType.TEXT),
        ("author_name", DataType.TEXT),
        ("message_ts", DataType.TEXT),
        ("thread_ts", DataType.TEXT),
        ("source_message_id", DataType.TEXT),
        ("topic_tags", DataType.TEXT_ARRAY),
        ("entity_tags", DataType.TEXT_ARRAY),
        ("action_tags", DataType.TEXT_ARRAY),
        ("importance", DataType.TEXT),
        ("graph_entity_ids", DataType.TEXT_ARRAY),
        ("source_media_url", DataType.TEXT),
        ("source_media_type", DataType.TEXT),
        ("source_media_urls", DataType.TEXT_ARRAY),
        ("source_link_urls", DataType.TEXT_ARRAY),
        ("source_link_titles", DataType.TEXT_ARRAY),
        ("source_link_descriptions", DataType.TEXT_ARRAY),
        ("valid_at", DataType.DATE),
        ("invalid_at", DataType.DATE),
        ("superseded_by", DataType.TEXT),
        ("supersedes", DataType.TEXT),
        ("potential_contradiction", DataType.BOOL),
        ("member_ids", DataType.TEXT_ARRAY),
        ("member_count", DataType.INT),
        ("fact_type", DataType.TEXT),
        ("thread_context_summary", DataType.TEXT),
        ("source_media_names", DataType.TEXT_ARRAY),
        # Enrichment fields (R4)
        ("authors", DataType.TEXT_ARRAY),
        ("date_range_start", DataType.TEXT),
        ("date_range_end", DataType.TEXT),
        ("high_importance_count", DataType.INT),
        ("key_entities_json", DataType.TEXT),
        ("key_relationships_json", DataType.TEXT),
        ("key_decisions_json", DataType.TEXT),
        ("key_topics_json", DataType.TEXT),
        ("media_refs", DataType.TEXT_ARRAY),
        ("media_names", DataType.TEXT_ARRAY),
        ("link_refs", DataType.TEXT_ARRAY),
        ("author_count", DataType.INT),
        ("media_count", DataType.INT),
        ("related_cluster_ids", DataType.TEXT_ARRAY),
        ("staleness_score", DataType.NUMBER),
        ("status", DataType.TEXT),
        ("fact_type_counts_json", DataType.TEXT),
        ("worst_staleness", DataType.NUMBER),
        # EntityKnowledgeCard fields
        ("entity_id", DataType.TEXT),
        ("entity_name", DataType.TEXT),
        ("entity_type", DataType.TEXT),
        ("channel_ids", DataType.TEXT_ARRAY),
        ("cluster_ids", DataType.TEXT_ARRAY),
        ("fact_count", DataType.INT),
        ("fact_type_breakdown_json", DataType.TEXT),
        ("key_facts", DataType.TEXT_ARRAY),
        ("related_entities_json", DataType.TEXT),
        ("last_mentioned_at", DataType.TEXT),
    ]

    async def ensure_schema(self) -> None:
        """Create or migrate the MemoryFact collection."""

        def _ensure() -> None:
            assert self._client is not None
            if self._client.collections.exists(COLLECTION_NAME):
                # Auto-migrate: add any missing properties to existing collections.
                collection = self._client.collections.get(COLLECTION_NAME)
                existing_names = {p.name for p in collection.config.get().properties}
                for prop_name, prop_type in self._EXPECTED_PROPERTIES:
                    if prop_name not in existing_names:
                        collection.config.add_property(
                            Property(name=prop_name, data_type=prop_type)
                        )
                        logger.info(
                            "WeaviateStore: added missing property '%s' to %s",
                            prop_name,
                            COLLECTION_NAME,
                        )
                return
            self._client.collections.create(
                name=COLLECTION_NAME,
                vectorizer_config=Configure.Vectorizer.none(),
                vector_index_config=Configure.VectorIndex.hnsw(),
                properties=[
                    Property(name=name, data_type=dtype)
                    for name, dtype in self._EXPECTED_PROPERTIES
                ],
            )

        await asyncio.to_thread(_ensure)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _collection(self):  # type: ignore[return]
        assert self._client is not None, "WeaviateStore not started"
        # Auto-create collection if it was deleted (e.g., during development resets)
        if not self._client.collections.exists(COLLECTION_NAME):
            logger.warning("WeaviateStore: collection %s missing, recreating schema", COLLECTION_NAME)
            self._ensure_schema_sync()
        return self._client.collections.get(COLLECTION_NAME)

    def _ensure_schema_sync(self) -> None:
        """Synchronous version of ensure_schema for use within _collection()."""
        assert self._client is not None
        if self._client.collections.exists(COLLECTION_NAME):
            return
        self._client.collections.create(
            name=COLLECTION_NAME,
            vectorizer_config=Configure.Vectorizer.none(),
            vector_index_config=Configure.VectorIndex.hnsw(),
            properties=[
                Property(name=name, data_type=dtype)
                for name, dtype in self._EXPECTED_PROPERTIES
            ],
        )

    @staticmethod
    def _coerce_date(value: Any) -> datetime | None:
        """Coerce a value to a timezone-aware datetime for Weaviate DATE fields.

        Returns None (which Weaviate treats as unset) if the value cannot be parsed.
        """
        if value is None:
            return None
        if isinstance(value, datetime):
            if value.tzinfo is None:
                from datetime import timezone
                return value.replace(tzinfo=timezone.utc)
            return value
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    from datetime import timezone
                    return parsed.replace(tzinfo=timezone.utc)
                return parsed
            except (ValueError, TypeError):
                logger.warning("WeaviateStore: could not parse date value: %r", value)
                return None
        return None

    @staticmethod
    def _fact_to_properties(fact: AtomicFact) -> dict[str, Any]:
        """Convert an AtomicFact to a Weaviate property dict."""
        props: dict[str, Any] = {
            "memory_text": fact.memory_text,
            "quality_score": fact.quality_score,
            "tier": fact.tier,
            "cluster_id": fact.cluster_id or "__none__",
            "channel_id": fact.channel_id,
            "platform": fact.platform,
            "author_id": fact.author_id,
            "author_name": fact.author_name,
            "message_ts": fact.message_ts,
            "thread_ts": fact.thread_ts or "",
            "source_message_id": fact.source_message_id,
            "topic_tags": fact.topic_tags,
            "entity_tags": fact.entity_tags,
            "action_tags": fact.action_tags,
            "importance": fact.importance,
            "graph_entity_ids": fact.graph_entity_ids,
            "source_media_url": fact.source_media_url,
            "source_media_type": fact.source_media_type,
            "source_media_urls": fact.source_media_urls,
            "source_link_urls": fact.source_link_urls,
            "source_link_titles": fact.source_link_titles,
            "source_link_descriptions": fact.source_link_descriptions,
            "fact_type": fact.fact_type,
            "thread_context_summary": fact.thread_context_summary,
            "source_media_names": fact.source_media_names,
        }
        # Supersession fields
        if fact.superseded_by:
            props["superseded_by"] = fact.superseded_by
        if fact.supersedes:
            props["supersedes"] = fact.supersedes
        props["potential_contradiction"] = fact.potential_contradiction
        # Weaviate DATE fields require proper datetime objects or must be omitted.
        valid_at = WeaviateStore._coerce_date(fact.valid_at)
        if valid_at is not None:
            props["valid_at"] = valid_at
        invalid_at = WeaviateStore._coerce_date(fact.invalid_at)
        if invalid_at is not None:
            props["invalid_at"] = invalid_at
        return props

    @staticmethod
    def _obj_to_fact(obj: Any, include_vector: bool = False) -> AtomicFact:
        """Convert a Weaviate data object back to an AtomicFact."""
        props = obj.properties
        fact = AtomicFact(
            id=str(obj.uuid),
            memory_text=props.get("memory_text", ""),
            quality_score=float(props.get("quality_score", 0.0)),
            tier=props.get("tier", "atomic"),
            cluster_id=props.get("cluster_id") or None,
            channel_id=props.get("channel_id", ""),
            platform=props.get("platform", "slack"),
            author_id=props.get("author_id", ""),
            author_name=props.get("author_name", ""),
            message_ts=props.get("message_ts", ""),
            thread_ts=props.get("thread_ts") or None,
            source_message_id=props.get("source_message_id", ""),
            topic_tags=props.get("topic_tags") or [],
            entity_tags=props.get("entity_tags") or [],
            action_tags=props.get("action_tags") or [],
            importance=props.get("importance", "medium"),
            graph_entity_ids=props.get("graph_entity_ids") or [],
            source_media_url=props.get("source_media_url", ""),
            source_media_type=props.get("source_media_type", ""),
            source_media_urls=props.get("source_media_urls") or [],
            source_media_names=props.get("source_media_names") or [],
            source_link_urls=props.get("source_link_urls") or [],
            source_link_titles=props.get("source_link_titles") or [],
            source_link_descriptions=props.get("source_link_descriptions") or [],
            fact_type=props.get("fact_type", ""),
            thread_context_summary=props.get("thread_context_summary", ""),
            valid_at=props.get("valid_at"),
            invalid_at=props.get("invalid_at"),
            superseded_by=props.get("superseded_by") or None,
            supersedes=props.get("supersedes") or None,
            potential_contradiction=bool(props.get("potential_contradiction")),
        )
        if include_vector and hasattr(obj, "vector") and obj.vector:
            vec = obj.vector
            if isinstance(vec, dict):
                vec = vec.get("default", [])
            fact.text_vector = vec
        return fact

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    async def upsert_fact(self, fact: AtomicFact) -> str:
        """Upsert a single AtomicFact. Returns the fact id."""

        def _upsert() -> str:
            collection = self._collection()
            # Use replace() instead of insert() for idempotent upsert semantics.
            # replace() creates the object if the UUID does not exist, or fully
            # replaces it if it does — safe to call multiple times with the same
            # deterministic UUID.
            collection.data.replace(
                properties=self._fact_to_properties(fact),
                uuid=fact.id,
                vector=fact.text_vector or None,
            )
            return fact.id

        return await asyncio.to_thread(_upsert)

    async def batch_upsert_facts(self, facts: list[AtomicFact]) -> list[str]:
        """Batch upsert multiple AtomicFacts. Returns list of ids."""

        def _batch() -> list[str]:
            collection = self._collection()
            ids: list[str] = []
            try:
                with collection.batch.dynamic() as batch:
                    for fact in facts:
                        props = self._fact_to_properties(fact)
                        batch.add_object(
                            properties=props,
                            uuid=fact.id,
                            vector=fact.text_vector or None,
                        )
                        ids.append(fact.id)
            except Exception as exc:  # noqa: BLE001
                # Weaviate populates failed_objects AFTER the context manager
                # exits, so inspect them here for detailed per-object errors.
                failed = list(getattr(collection.batch, "failed_objects", []) or [])
                if failed:
                    logger.error(
                        "WeaviateStore: %d/%d objects failed in batch upsert",
                        len(failed),
                        len(ids),
                    )
                    for i, obj in enumerate(failed[:5]):
                        logger.error(
                            "  failed[%d]: uuid=%s error=%s",
                            i,
                            getattr(obj, "original_uuid", "?"),
                            getattr(obj, "message", str(obj)),
                        )
                else:
                    logger.error(
                        "WeaviateStore: batch failed with no failed_objects detail: %s",
                        exc,
                    )
                # Log a sample fact's property keys/types (not values — vectors are huge).
                if facts:
                    sample = self._fact_to_properties(facts[0])
                    logger.error(
                        "WeaviateStore: sample fact property keys/types: %s",
                        {k: type(v).__name__ for k, v in sample.items()},
                    )
                raise RuntimeError(
                    "Weaviate batch_upsert_facts failed for %d facts (sample_ids=%s): %s"
                    % (len(ids), ids[:3], exc)
                ) from exc

            # Also check after successful exit (some Weaviate versions don't raise).
            failed = list(getattr(collection.batch, "failed_objects", []) or [])
            if failed:
                error_messages: list[str] = []
                for i, obj in enumerate(failed[:5]):
                    msg = getattr(obj, "message", None) or "unknown"
                    uid = getattr(obj, "original_uuid", "?")
                    error_messages.append(f"uuid={uid}: {msg}")
                    logger.error("  WeaviateStore failed[%d]: %s", i, error_messages[-1])
                # Log sample properties WITHOUT vectors for debugging.
                if facts:
                    sample = self._fact_to_properties(facts[0])
                    logger.error(
                        "WeaviateStore: sample fact property keys/types: %s",
                        {k: type(v).__name__ for k, v in sample.items()},
                    )
                raise RuntimeError(
                    "Weaviate batch: %d/%d objects failed. First errors: %s"
                    % (len(failed), len(ids), "; ".join(error_messages[:3]))
                )

            logger.info("WeaviateStore: batch upsert succeeded for %d facts", len(ids))
            return ids

        return await asyncio.to_thread(_batch)

    async def update_fact_cluster(self, fact_id: str, cluster_id: str) -> None:
        """Update the cluster_id field of an existing fact."""

        def _update() -> None:
            collection = self._collection()
            collection.data.update(
                uuid=fact_id,
                properties={"cluster_id": cluster_id},
            )

        await asyncio.to_thread(_update)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    async def get_fact(self, fact_id: str) -> AtomicFact | None:
        """Fetch a single fact by id. Returns None if not found."""

        def _get() -> AtomicFact | None:
            collection = self._collection()
            obj = collection.query.fetch_object_by_id(uuid=fact_id)
            if obj is None:
                return None
            return self._obj_to_fact(obj)

        return await asyncio.to_thread(_get)

    async def list_facts(
        self,
        channel_id: str,
        filters: MemoryFilters,
        page: int = 1,
        limit: int = 20,
    ) -> PaginatedFacts:
        """Return a paginated list of facts filtered by channel and optional criteria."""

        def _list() -> PaginatedFacts:
            collection = self._collection()

            # Build filter chain starting with channel_id (always required)
            weaviate_filter: Any = (
                Filter.by_property("channel_id").equal(channel_id)
                & Filter.by_property("tier").equal("atomic")
            )

            if filters.topic:
                weaviate_filter = weaviate_filter & Filter.by_property("topic_tags").contains_any(
                    [filters.topic]
                )
            if filters.entity:
                weaviate_filter = weaviate_filter & Filter.by_property("entity_tags").contains_any(
                    [filters.entity]
                )
            if filters.importance:
                weaviate_filter = weaviate_filter & Filter.by_property("importance").equal(
                    filters.importance
                )
            if filters.since:
                since_dt = datetime.fromisoformat(filters.since)
                weaviate_filter = weaviate_filter & Filter.by_property("valid_at").greater_or_equal(
                    since_dt
                )
            if filters.until:
                until_dt = datetime.fromisoformat(filters.until)
                weaviate_filter = weaviate_filter & Filter.by_property("valid_at").less_or_equal(
                    until_dt
                )

            offset = (page - 1) * limit

            result = collection.query.fetch_objects(
                filters=weaviate_filter,
                limit=limit,
                offset=offset,
            )

            # Count total matching objects for pagination metadata
            count_result = collection.aggregate.over_all(
                filters=weaviate_filter,
                total_count=True,
            )
            total = count_result.total_count or 0

            facts = [self._obj_to_fact(obj) for obj in result.objects]
            pages = max(1, math.ceil(total / limit))

            return PaginatedFacts(
                memories=facts,
                total=total,
                page=page,
                pages=pages,
            )

        return await asyncio.to_thread(_list)

    async def count_facts(self, channel_id: str | None = None) -> int:
        """Return total count of facts, optionally scoped to a channel."""

        def _count() -> int:
            collection = self._collection()
            tier_filter = Filter.by_property("tier").equal("atomic")
            weaviate_filter = (
                Filter.by_property("channel_id").equal(channel_id) & tier_filter
                if channel_id
                else tier_filter
            )
            result = collection.aggregate.over_all(
                filters=weaviate_filter,
                total_count=True,
            )
            return result.total_count or 0

        return await asyncio.to_thread(_count)

    async def delete_by_channel(self, channel_id: str) -> int:
        """Delete all objects for a channel (facts, clusters, summaries).

        Returns count of deleted objects.
        """

        def _delete() -> int:
            collection = self._collection()
            # Delete ALL tiers for this channel — atomic facts, topic clusters, summaries
            result = collection.query.fetch_objects(
                filters=Filter.by_property("channel_id").equal(channel_id),
                limit=10000,
            )
            ids = [obj.uuid for obj in result.objects]
            for uid in ids:
                collection.data.delete_by_id(uuid=uid)
            return len(ids)

        return await asyncio.to_thread(_delete)

    async def delete_all(self) -> int:
        """Delete ALL objects in the collection. Dev/reset use only."""

        def _delete_all() -> int:
            collection = self._collection()
            result = collection.query.fetch_objects(limit=10000)
            ids = [obj.uuid for obj in result.objects]
            for uid in ids:
                collection.data.delete_by_id(uuid=uid)
            return len(ids)

        return await asyncio.to_thread(_delete_all)

    # ------------------------------------------------------------------
    # Semantic search
    # ------------------------------------------------------------------

    async def semantic_search(
        self,
        query_vector: list[float],
        channel_id: str | None = None,
        filters: Any = None,
        limit: int = 20,
        threshold: float = 0.7,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Search facts by vector similarity using Weaviate near_vector.

        Returns list of dicts with ``fact`` (AtomicFact) and ``similarity_score``.
        """
        from weaviate.classes.query import MetadataQuery

        def _search() -> list[dict[str, Any]]:
            collection = self._collection()

            # Build filter
            weaviate_filter = None
            if channel_id:
                weaviate_filter = Filter.by_property("channel_id").equal(channel_id)

            # Exclude cluster/summary objects from fact search
            tier_filter = Filter.by_property("tier").equal("atomic")
            weaviate_filter = (
                weaviate_filter & tier_filter if weaviate_filter else tier_filter
            )

            result = collection.query.near_vector(
                near_vector=query_vector,
                limit=limit,
                filters=weaviate_filter,
                return_metadata=MetadataQuery(distance=True),
            )

            results: list[dict[str, Any]] = []
            for obj in result.objects:
                # Weaviate returns distance (lower = more similar).
                # Convert to similarity score: 1 - distance (for cosine).
                distance = getattr(obj.metadata, "distance", None)
                similarity = 1.0 - (distance if distance is not None else 1.0)
                if similarity < threshold:
                    continue
                fact = self._obj_to_fact(obj)
                # Post-filter superseded facts Python-side (avoids is_none nullstate
                # indexing requirement that Weaviate rejects when nullstate is not
                # indexed in the schema).
                if not include_superseded and fact.invalid_at is not None:
                    continue
                results.append({
                    "fact": fact,
                    "similarity_score": round(similarity, 4),
                })
            return results

        return await asyncio.to_thread(_search)

    async def bm25_search(
        self,
        query: str,
        channel_id: str,
        tier: str = "atomic",
        limit: int = 10,
    ) -> list[AtomicFact]:
        """BM25 keyword search over MemoryFact, scoped to a channel and tier."""

        def _search() -> list[AtomicFact]:
            collection = self._collection()
            channel_filter = Filter.by_property("channel_id").equal(channel_id)
            tier_filter = Filter.by_property("tier").equal(tier)
            combined = channel_filter & tier_filter
            result = collection.query.bm25(
                query=query,
                limit=limit,
                filters=combined,
            )
            return [self._obj_to_fact(obj) for obj in result.objects]

        try:
            return await asyncio.to_thread(_search)
        except Exception:
            logger.exception("WeaviateStore.bm25_search failed for query=%r channel=%s", query, channel_id)
            return []

    async def true_hybrid_search(
        self,
        query_text: str,
        query_vector: list[float],
        channel_id: str,
        tier: str = "atomic",
        limit: int = 20,
        alpha: float | None = None,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Weaviate v4 native hybrid search combining BM25 + vector via Weaviate's
        built-in fusion (Ranked Fusion by default).

        Unlike ``pseudo_hybrid_search``, this issues a single Weaviate
        ``collection.query.hybrid()`` call, so both the BM25 and vector scores
        are fused server-side rather than merged client-side.

        Args:
            query_text: Raw text for the BM25 side.
            query_vector: Pre-computed embedding for the vector side.  Required
                because the MemoryFact collection uses ``Vectorizer.none()``.
            channel_id: Scope results to this channel.
            tier: Weaviate ``tier`` property filter (default ``"atomic"``).
            limit: Maximum results to return.
            alpha: BM25/vector blend (0.0 = pure BM25, 1.0 = pure vector).
                Defaults to ``settings.weaviate_hybrid_alpha`` (0.6).
            include_superseded: When False (default), exclude facts with a
                non-null ``invalid_at`` field.

        Returns:
            Same shape as ``bm25_search`` / ``semantic_search``:
            list of ``{"fact": AtomicFact, "similarity_score": float}``.
        """
        from beever_atlas.infra.config import get_settings
        from weaviate.classes.query import MetadataQuery

        resolved_alpha = alpha if alpha is not None else get_settings().weaviate_hybrid_alpha

        def _search() -> list[dict[str, Any]]:
            collection = self._collection()

            weaviate_filter: Any = Filter.by_property("channel_id").equal(channel_id)
            tier_filter = Filter.by_property("tier").equal(tier)
            weaviate_filter = weaviate_filter & tier_filter

            result = collection.query.hybrid(
                query=query_text,
                vector=query_vector,
                alpha=resolved_alpha,
                limit=limit,
                filters=weaviate_filter,
                return_metadata=MetadataQuery(score=True),
            )

            results: list[dict[str, Any]] = []
            for obj in result.objects:
                # Hybrid score is already 0-1 (Weaviate Ranked Fusion).
                # Do not apply a distance threshold here — hybrid scores are not
                # distances and the 0.7 cutoff in semantic_search is for cosine
                # distance, not hybrid score.
                score = getattr(obj.metadata, "score", None) or 0.0
                fact = self._obj_to_fact(obj)
                # Post-filter superseded facts Python-side (avoids is_none nullstate
                # indexing requirement that Weaviate rejects when nullstate is not
                # indexed in the schema).
                if not include_superseded and fact.invalid_at is not None:
                    continue
                results.append({
                    "fact": fact,
                    "similarity_score": round(float(score), 4),
                })
            return results

        try:
            return await asyncio.to_thread(_search)
        except Exception:
            logger.exception(
                "WeaviateStore.true_hybrid_search failed for query=%r channel=%s",
                query_text,
                channel_id,
            )
            return []

    # Legacy pseudo-hybrid: vector search + field-filter merge (client-side).
    # Kept for api/search.py backward compatibility.  New code should use
    # true_hybrid_search() for real BM25+vector fusion via Weaviate.
    async def pseudo_hybrid_search(
        self,
        query_vector: list[float],
        channel_id: str,
        filters: Any = None,
        limit: int = 20,
        threshold: float = 0.7,
        include_superseded: bool = False,
    ) -> list[dict[str, Any]]:
        """Merge semantic vector results with field-filter results, deduplicated.

        Returns list of dicts with ``fact`` and ``similarity_score``.
        Overlapping facts (found by both methods) are ranked highest.

        .. deprecated::
            This is a client-side merge, NOT a real hybrid search.  It has no
            BM25 component and no ``alpha`` parameter.  Use
            ``true_hybrid_search()`` for Weaviate-native BM25+vector fusion.
        """
        # Run both searches
        vector_results = await self.semantic_search(
            query_vector=query_vector,
            channel_id=channel_id,
            limit=limit,
            threshold=threshold,
            include_superseded=include_superseded,
        )

        # Field-filter results (existing exact search)
        from beever_atlas.models import MemoryFilters
        field_result = await self.list_facts(
            channel_id=channel_id,
            filters=filters or MemoryFilters(),
            page=1,
            limit=limit,
        )

        # Merge and deduplicate
        seen_ids: set[str] = set()
        merged: list[dict[str, Any]] = []

        # Vector results first (already have similarity scores)
        vector_ids: set[str] = set()
        for vr in vector_results:
            fact = vr["fact"]
            vector_ids.add(fact.id)
            seen_ids.add(fact.id)
            merged.append(vr)

        # Field-filter results — boost score if also found by vector search
        for fact in field_result.memories:
            if include_superseded is False and fact.invalid_at is not None:
                continue
            if fact.id in seen_ids:
                # Already in results from vector search — boost it
                for item in merged:
                    if item["fact"].id == fact.id:
                        item["similarity_score"] = min(1.0, item["similarity_score"] + 0.1)
                        break
                continue
            seen_ids.add(fact.id)
            merged.append({
                "fact": fact,
                "similarity_score": 0.5,  # Default score for field-filter matches
            })

        # Sort by similarity score descending
        merged.sort(key=lambda x: x["similarity_score"], reverse=True)
        return merged[:limit]

    async def supersede_fact(
        self,
        old_fact_id: str,
        new_fact_id: str,
    ) -> None:
        """Mark an old fact as superseded by a new fact.

        Sets ``invalid_at`` and ``superseded_by`` on the old fact,
        and ``supersedes`` on the new fact.
        """
        from datetime import timezone

        now = datetime.now(tz=timezone.utc)

        def _supersede() -> None:
            collection = self._collection()
            # Update old fact
            collection.data.update(
                uuid=old_fact_id,
                properties={
                    "invalid_at": now,
                    "superseded_by": new_fact_id,
                },
            )
            # Update new fact
            collection.data.update(
                uuid=new_fact_id,
                properties={
                    "supersedes": old_fact_id,
                },
            )

        await asyncio.to_thread(_supersede)

    async def flag_potential_contradiction(self, fact_id: str) -> None:
        """Flag a fact as having a potential contradiction."""

        def _flag() -> None:
            collection = self._collection()
            collection.data.update(
                uuid=fact_id,
                properties={"potential_contradiction": True},
            )

        await asyncio.to_thread(_flag)

    async def fetch_by_ids(self, fact_ids: list[str]) -> list[AtomicFact]:
        """Fetch multiple facts by their ids. Skips ids that are not found."""

        def _fetch() -> list[AtomicFact]:
            collection = self._collection()
            facts: list[AtomicFact] = []
            for fid in fact_ids:
                obj = collection.query.fetch_object_by_id(uuid=fid)
                if obj is not None:
                    facts.append(self._obj_to_fact(obj))
            return facts

        return await asyncio.to_thread(_fetch)

    # ------------------------------------------------------------------
    # Cluster / summary operations (Tier 0 + Tier 1)
    # ------------------------------------------------------------------

    async def get_unclustered_facts(
        self, channel_id: str, limit: int | None = None,
    ) -> list[AtomicFact]:
        """Fetch atomic facts that have no cluster assignment, with vectors.

        Streams via cursor pagination so a single gRPC response never exceeds
        Weaviate's 10MB cap. Pass ``limit`` to stop early; ``None`` drains all.
        """
        facts: list[AtomicFact] = []
        async for fact in self.iter_unclustered_facts(channel_id):
            facts.append(fact)
            if limit is not None and len(facts) >= limit:
                break
        return facts

    async def iter_unclustered_facts(
        self, channel_id: str, page_size: int = 200,
    ) -> AsyncIterator[AtomicFact]:
        """Yield unclustered atomic facts (with vectors), one page at a time.

        Uses offset pagination with small pages so each gRPC response stays well
        under Weaviate's 10MB cap. (Cursor ``after=`` is unavailable here because
        Weaviate rejects ``after`` combined with a ``where`` filter.)

        Note: Weaviate's ``QUERY_MAXIMUM_RESULTS`` (default 10000) caps the total
        number of results an offset query can traverse. Channels above that size
        require a schema-level bump of that setting.
        """
        weaviate_filter = (
            Filter.by_property("channel_id").equal(channel_id)
            & Filter.by_property("tier").equal("atomic")
            & Filter.by_property("cluster_id").equal("__none__")
        )

        def _fetch_page(offset: int) -> list[Any]:
            collection = self._collection()
            return list(collection.query.fetch_objects(
                filters=weaviate_filter,
                limit=page_size,
                offset=offset,
                include_vector=True,
            ).objects)

        offset = 0
        while True:
            page = await asyncio.to_thread(_fetch_page, offset)
            if not page:
                return
            for obj in page:
                yield self._obj_to_fact(obj, include_vector=True)
            if len(page) < page_size:
                return
            offset += page_size

    async def iter_all_fact_ids(
        self, channel_id: str, page_size: int = 500,
    ) -> AsyncIterator[tuple[str, str]]:
        """Yield ``(uuid, cluster_id)`` pairs for every atomic fact in a channel.

        Returns only the two fields needed for cluster resets — no vectors, no
        text — so each page is a few KB. Uses offset pagination (cursor ``after``
        is incompatible with filters in Weaviate).
        """
        weaviate_filter = (
            Filter.by_property("channel_id").equal(channel_id)
            & Filter.by_property("tier").equal("atomic")
        )

        def _fetch_page(offset: int) -> list[Any]:
            collection = self._collection()
            return list(collection.query.fetch_objects(
                filters=weaviate_filter,
                limit=page_size,
                offset=offset,
                return_properties=["cluster_id"],
            ).objects)

        offset = 0
        while True:
            page = await asyncio.to_thread(_fetch_page, offset)
            if not page:
                return
            for obj in page:
                yield str(obj.uuid), obj.properties.get("cluster_id") or ""
            if len(page) < page_size:
                return
            offset += page_size

    async def upsert_cluster(self, cluster: "TopicCluster") -> str:
        """Upsert a topic cluster as a MemoryFact with tier='topic'."""

        def _upsert() -> str:
            collection = self._collection()
            props: dict[str, Any] = {
                "memory_text": cluster.summary,
                "tier": "topic",
                "cluster_id": "",
                "channel_id": cluster.channel_id,
                "topic_tags": cluster.topic_tags,
                "member_ids": cluster.member_ids,
                "member_count": cluster.member_count,
                "platform": "",
                "author_id": "",
                "author_name": "",
                "message_ts": "",
                "thread_ts": "",
                "source_message_id": "",
                "entity_tags": [],
                "action_tags": [],
                "importance": "",
                "graph_entity_ids": [],
                "source_media_url": "",
                "source_media_type": "",
                "source_media_urls": [],
                "source_link_urls": [],
                "source_link_titles": [],
                "source_link_descriptions": [],
                "quality_score": 0.0,
                "potential_contradiction": False,
                # Enrichment fields (R4)
                "authors": cluster.authors,
                "date_range_start": cluster.date_range_start,
                "date_range_end": cluster.date_range_end,
                "high_importance_count": cluster.high_importance_count,
                "key_entities_json": json.dumps(cluster.key_entities),
                "key_relationships_json": json.dumps(cluster.key_relationships),
                "media_refs": cluster.media_refs,
                "media_names": cluster.media_names,
                "link_refs": cluster.link_refs,
                "related_cluster_ids": cluster.related_cluster_ids,
                "staleness_score": cluster.staleness_score,
                "status": cluster.status,
                "fact_type_counts_json": json.dumps(cluster.fact_type_counts),
                # Wiki-ready enrichment fields
                "title": cluster.title,
                "current_state": cluster.current_state,
                "open_questions": cluster.open_questions,
                "impact_note": cluster.impact_note,
                "key_facts_json": json.dumps(cluster.key_facts),
                "decisions_json": json.dumps(cluster.decisions),
                "people_json": json.dumps(cluster.people),
                "technologies_json": json.dumps(cluster.technologies),
                "projects_json": json.dumps(cluster.projects),
                "faq_candidates_json": json.dumps(cluster.faq_candidates),
            }
            try:
                collection.data.insert(
                    properties=props,
                    uuid=cluster.id,
                    vector=cluster.centroid_vector or None,
                )
            except Exception:
                # Object already exists — update it
                collection.data.replace(
                    properties=props,
                    uuid=cluster.id,
                    vector=cluster.centroid_vector or None,
                )
            return cluster.id

        return await asyncio.to_thread(_upsert)

    async def list_clusters(self, channel_id: str) -> list["TopicCluster"]:
        """List all topic clusters for a channel, with centroid vectors."""
        from beever_atlas.models import TopicCluster

        def _list() -> list[TopicCluster]:
            collection = self._collection()
            result = collection.query.fetch_objects(
                filters=(
                    Filter.by_property("channel_id").equal(channel_id)
                    & Filter.by_property("tier").equal("topic")
                ),
                limit=500,
                include_vector=True,
            )
            clusters: list[TopicCluster] = []
            for obj in result.objects:
                props = obj.properties
                vec = obj.vector
                if isinstance(vec, dict):
                    vec = vec.get("default", [])
                clusters.append(TopicCluster(
                    id=str(obj.uuid),
                    channel_id=props.get("channel_id", ""),
                    title=props.get("title", ""),
                    summary=props.get("memory_text", ""),
                    current_state=props.get("current_state", ""),
                    open_questions=props.get("open_questions", ""),
                    impact_note=props.get("impact_note", ""),
                    topic_tags=props.get("topic_tags") or [],
                    member_ids=props.get("member_ids") or [],
                    member_count=int(props.get("member_count", 0)),
                    centroid_vector=vec if vec else None,
                    key_entities=json.loads(props.get("key_entities_json") or "[]"),
                    key_relationships=json.loads(props.get("key_relationships_json") or "[]"),
                    date_range_start=props.get("date_range_start", ""),
                    date_range_end=props.get("date_range_end", ""),
                    authors=props.get("authors") or [],
                    media_refs=props.get("media_refs") or [],
                    media_names=props.get("media_names") or [],
                    link_refs=props.get("link_refs") or [],
                    high_importance_count=int(props.get("high_importance_count", 0)),
                    related_cluster_ids=props.get("related_cluster_ids") or [],
                    staleness_score=float(props.get("staleness_score", 0.0)),
                    status=props.get("status", "active"),
                    fact_type_counts=json.loads(props.get("fact_type_counts_json") or "{}"),
                    key_facts=json.loads(props.get("key_facts_json") or "[]"),
                    decisions=json.loads(props.get("decisions_json") or "[]"),
                    people=json.loads(props.get("people_json") or "[]"),
                    technologies=json.loads(props.get("technologies_json") or "[]"),
                    projects=json.loads(props.get("projects_json") or "[]"),
                    faq_candidates=json.loads(props.get("faq_candidates_json") or "[]"),
                ))
            return clusters

        return await asyncio.to_thread(_list)

    async def get_cluster(self, cluster_id: str) -> "TopicCluster | None":
        """Fetch a single topic cluster by ID, with centroid vector."""
        from beever_atlas.models import TopicCluster

        def _get() -> TopicCluster | None:
            collection = self._collection()
            obj = collection.query.fetch_object_by_id(
                uuid=cluster_id,
                include_vector=True,
            )
            if obj is None:
                return None
            props = obj.properties
            if props.get("tier") != "topic":
                return None
            vec = obj.vector
            if isinstance(vec, dict):
                vec = vec.get("default", [])
            return TopicCluster(
                id=str(obj.uuid),
                channel_id=props.get("channel_id", ""),
                title=props.get("title", ""),
                summary=props.get("memory_text", ""),
                current_state=props.get("current_state", ""),
                open_questions=props.get("open_questions", ""),
                impact_note=props.get("impact_note", ""),
                topic_tags=props.get("topic_tags") or [],
                member_ids=props.get("member_ids") or [],
                member_count=int(props.get("member_count", 0)),
                centroid_vector=vec if vec else None,
                key_entities=json.loads(props.get("key_entities_json") or "[]"),
                key_relationships=json.loads(props.get("key_relationships_json") or "[]"),
                date_range_start=props.get("date_range_start", ""),
                date_range_end=props.get("date_range_end", ""),
                authors=props.get("authors") or [],
                media_refs=props.get("media_refs") or [],
                media_names=props.get("media_names") or [],
                link_refs=props.get("link_refs") or [],
                high_importance_count=int(props.get("high_importance_count", 0)),
                related_cluster_ids=props.get("related_cluster_ids") or [],
                staleness_score=float(props.get("staleness_score", 0.0)),
                status=props.get("status", "active"),
                fact_type_counts=json.loads(props.get("fact_type_counts_json") or "{}"),
                key_facts=json.loads(props.get("key_facts_json") or "[]"),
                decisions=json.loads(props.get("decisions_json") or "[]"),
                people=json.loads(props.get("people_json") or "[]"),
                technologies=json.loads(props.get("technologies_json") or "[]"),
                projects=json.loads(props.get("projects_json") or "[]"),
                faq_candidates=json.loads(props.get("faq_candidates_json") or "[]"),
            )

        return await asyncio.to_thread(_get)

    async def get_cluster_members(
        self, cluster_id: str, limit: int = 100,
    ) -> list[AtomicFact]:
        """Fetch atomic facts assigned to a specific cluster."""

        def _fetch() -> list[AtomicFact]:
            collection = self._collection()
            result = collection.query.fetch_objects(
                filters=(
                    Filter.by_property("cluster_id").equal(cluster_id)
                    & Filter.by_property("tier").equal("atomic")
                ),
                limit=limit,
            )
            return [self._obj_to_fact(obj) for obj in result.objects]

        return await asyncio.to_thread(_fetch)

    async def upsert_channel_summary(self, summary: "ChannelSummary") -> str:
        """Upsert a channel summary (Tier 0). One per channel via deterministic UUID."""

        def _upsert() -> str:
            collection = self._collection()
            # Deterministic UUID ensures exactly one summary per channel
            namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
            det_id = str(uuid.uuid5(namespace, f"summary:{summary.channel_id}"))
            props: dict[str, Any] = {
                "memory_text": summary.text,
                "tier": "summary",
                "cluster_id": "",
                "channel_id": summary.channel_id,
                "member_count": summary.cluster_count,
                "member_ids": [],
                "topic_tags": [],
                "platform": "",
                "author_id": "",
                "author_name": "",
                "message_ts": "",
                "thread_ts": "",
                "source_message_id": "",
                "entity_tags": [],
                "action_tags": [],
                "importance": "",
                "graph_entity_ids": [],
                "source_media_url": "",
                "source_media_type": "",
                "source_media_urls": [],
                "source_link_urls": [],
                "source_link_titles": [],
                "source_link_descriptions": [],
                "quality_score": 0.0,
                "potential_contradiction": False,
                # Enrichment fields (R4)
                "key_entities_json": json.dumps(summary.key_entities),
                "key_decisions_json": json.dumps(summary.key_decisions),
                "key_topics_json": json.dumps(summary.key_topics),
                "date_range_start": summary.date_range_start,
                "date_range_end": summary.date_range_end,
                "media_count": summary.media_count,
                "author_count": summary.author_count,
                "worst_staleness": summary.worst_staleness,
                "fact_count": summary.fact_count,
                # Wiki-ready enrichment fields
                "channel_name": summary.channel_name,
                "description": summary.description,
                "themes": summary.themes,
                "momentum": summary.momentum,
                "team_dynamics": summary.team_dynamics,
                "top_decisions_json": json.dumps(summary.top_decisions),
                "top_people_json": json.dumps(summary.top_people),
                "tech_stack_json": json.dumps(summary.tech_stack),
                "active_projects_json": json.dumps(summary.active_projects),
                "glossary_terms_json": json.dumps(summary.glossary_terms),
                "recent_activity_json": json.dumps(summary.recent_activity_summary),
                "topic_graph_edges_json": json.dumps(summary.topic_graph_edges),
            }
            try:
                collection.data.insert(
                    properties=props,
                    uuid=det_id,
                )
            except Exception:
                collection.data.replace(
                    properties=props,
                    uuid=det_id,
                )
            return det_id

        return await asyncio.to_thread(_upsert)

    async def get_channel_summary(self, channel_id: str) -> "ChannelSummary | None":
        """Fetch the Tier 0 channel summary."""
        from beever_atlas.models import ChannelSummary

        def _get() -> ChannelSummary | None:
            collection = self._collection()
            result = collection.query.fetch_objects(
                filters=(
                    Filter.by_property("channel_id").equal(channel_id)
                    & Filter.by_property("tier").equal("summary")
                ),
                limit=1,
            )
            if not result.objects:
                return None
            obj = result.objects[0]
            props = obj.properties
            return ChannelSummary(
                id=str(obj.uuid),
                channel_id=props.get("channel_id", ""),
                channel_name=props.get("channel_name", ""),
                text=props.get("memory_text", ""),
                description=props.get("description", ""),
                themes=props.get("themes", ""),
                momentum=props.get("momentum", ""),
                team_dynamics=props.get("team_dynamics", ""),
                cluster_count=int(props.get("member_count", 0)),
                fact_count=int(props.get("fact_count", 0)),
                key_decisions=json.loads(props.get("key_decisions_json") or "[]"),
                key_entities=json.loads(props.get("key_entities_json") or "[]"),
                key_topics=json.loads(props.get("key_topics_json") or "[]"),
                date_range_start=props.get("date_range_start", ""),
                date_range_end=props.get("date_range_end", ""),
                media_count=int(props.get("media_count", 0)),
                author_count=int(props.get("author_count", 0)),
                worst_staleness=float(props.get("worst_staleness", 0.0)),
                top_decisions=json.loads(props.get("top_decisions_json") or "[]"),
                top_people=json.loads(props.get("top_people_json") or "[]"),
                tech_stack=json.loads(props.get("tech_stack_json") or "[]"),
                active_projects=json.loads(props.get("active_projects_json") or "[]"),
                glossary_terms=json.loads(props.get("glossary_terms_json") or "[]"),
                recent_activity_summary=json.loads(props.get("recent_activity_json") or "{}"),
                topic_graph_edges=json.loads(props.get("topic_graph_edges_json") or "[]"),
            )

        return await asyncio.to_thread(_get)

    async def upsert_entity_card(self, card: "EntityKnowledgeCard") -> str:
        """Upsert an EntityKnowledgeCard as a MemoryFact with tier='entity_card'."""

        def _upsert() -> str:
            collection = self._collection()
            # Deterministic UUID from entity_name
            namespace = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")
            det_id = str(uuid.uuid5(namespace, f"entity_card:{card.entity_name}"))
            props: dict[str, Any] = {
                "memory_text": card.summary,
                "tier": "entity_card",
                "cluster_id": "",
                "channel_id": "",
                "entity_id": card.entity_id,
                "entity_name": card.entity_name,
                "entity_type": card.entity_type,
                "channel_ids": card.channel_ids,
                "cluster_ids": card.cluster_ids,
                "fact_count": card.fact_count,
                "fact_type_breakdown_json": json.dumps(card.fact_type_breakdown),
                "key_facts": card.key_facts,
                "related_entities_json": json.dumps(card.related_entities),
                "last_mentioned_at": card.last_mentioned_at,
                "staleness_score": card.staleness_score,
                "platform": "",
                "author_id": "",
                "author_name": "",
                "message_ts": "",
                "thread_ts": "",
                "source_message_id": "",
                "topic_tags": [],
                "entity_tags": [],
                "action_tags": [],
                "importance": "",
                "graph_entity_ids": [],
                "source_media_url": "",
                "source_media_type": "",
                "source_media_urls": [],
                "source_link_urls": [],
                "source_link_titles": [],
                "source_link_descriptions": [],
                "quality_score": 0.0,
                "potential_contradiction": False,
                "member_ids": [],
                "member_count": 0,
            }
            try:
                collection.data.insert(properties=props, uuid=det_id)
            except Exception:
                collection.data.replace(properties=props, uuid=det_id)
            return det_id

        return await asyncio.to_thread(_upsert)

    async def get_entity_card(self, entity_name: str) -> "EntityKnowledgeCard | None":
        """Fetch an EntityKnowledgeCard by entity_name."""
        from beever_atlas.models.domain import EntityKnowledgeCard

        def _get() -> EntityKnowledgeCard | None:
            collection = self._collection()
            result = collection.query.fetch_objects(
                filters=(
                    Filter.by_property("tier").equal("entity_card")
                    & Filter.by_property("entity_name").equal(entity_name)
                ),
                limit=1,
            )
            if not result.objects:
                return None
            obj = result.objects[0]
            props = obj.properties
            return EntityKnowledgeCard(
                id=str(obj.uuid),
                entity_id=props.get("entity_id", ""),
                entity_name=props.get("entity_name", ""),
                entity_type=props.get("entity_type", ""),
                channel_ids=props.get("channel_ids") or [],
                cluster_ids=props.get("cluster_ids") or [],
                fact_count=int(props.get("fact_count", 0)),
                fact_type_breakdown=json.loads(props.get("fact_type_breakdown_json") or "{}"),
                key_facts=props.get("key_facts") or [],
                related_entities=json.loads(props.get("related_entities_json") or "[]"),
                last_mentioned_at=props.get("last_mentioned_at", ""),
                staleness_score=float(props.get("staleness_score", 0.0)),
                summary=props.get("memory_text", ""),
            )

        return await asyncio.to_thread(_get)

    async def list_entity_cards(
        self, channel_id: str | None = None, limit: int = 50,
    ) -> list["EntityKnowledgeCard"]:
        """List EntityKnowledgeCards, optionally filtered by channel_id."""
        from beever_atlas.models.domain import EntityKnowledgeCard

        def _list() -> list[EntityKnowledgeCard]:
            collection = self._collection()
            weaviate_filter = Filter.by_property("tier").equal("entity_card")
            if channel_id:
                weaviate_filter = weaviate_filter & Filter.by_property(
                    "channel_ids"
                ).contains_any([channel_id])
            result = collection.query.fetch_objects(
                filters=weaviate_filter,
                limit=limit,
            )
            cards: list[EntityKnowledgeCard] = []
            for obj in result.objects:
                props = obj.properties
                cards.append(EntityKnowledgeCard(
                    id=str(obj.uuid),
                    entity_id=props.get("entity_id", ""),
                    entity_name=props.get("entity_name", ""),
                    entity_type=props.get("entity_type", ""),
                    channel_ids=props.get("channel_ids") or [],
                    cluster_ids=props.get("cluster_ids") or [],
                    fact_count=int(props.get("fact_count", 0)),
                    fact_type_breakdown=json.loads(props.get("fact_type_breakdown_json") or "{}"),
                    key_facts=props.get("key_facts") or [],
                    related_entities=json.loads(props.get("related_entities_json") or "[]"),
                    last_mentioned_at=props.get("last_mentioned_at", ""),
                    staleness_score=float(props.get("staleness_score", 0.0)),
                    summary=props.get("memory_text", ""),
                ))
            return cards

        return await asyncio.to_thread(_list)

    async def fetch_all_cluster_members(
        self, channel_id: str, cluster_id: str, limit: int = 500,
    ) -> list[AtomicFact]:
        """Fetch ALL Tier 2 AtomicFacts for a specific cluster in a channel."""

        def _fetch() -> list[AtomicFact]:
            collection = self._collection()
            result = collection.query.fetch_objects(
                filters=(
                    Filter.by_property("channel_id").equal(channel_id)
                    & Filter.by_property("cluster_id").equal(cluster_id)
                    & Filter.by_property("tier").equal("atomic")
                ),
                limit=limit,
            )
            return [self._obj_to_fact(obj) for obj in result.objects]

        return await asyncio.to_thread(_fetch)

    async def fetch_media_facts(
        self, channel_id: str, limit: int = 500,
    ) -> list[AtomicFact]:
        """Fetch facts with non-empty source_media_urls or source_link_urls."""

        def _fetch() -> list[AtomicFact]:
            collection = self._collection()
            # Weaviate doesn't support "array is not empty" directly,
            # so we fetch all channel facts and filter in Python.
            result = collection.query.fetch_objects(
                filters=(
                    Filter.by_property("channel_id").equal(channel_id)
                    & Filter.by_property("tier").equal("atomic")
                ),
                limit=limit,
            )
            facts: list[AtomicFact] = []
            for obj in result.objects:
                fact = self._obj_to_fact(obj)
                if fact.source_media_urls or fact.source_link_urls:
                    facts.append(fact)
            return facts

        return await asyncio.to_thread(_fetch)

    async def fetch_recent_facts(
        self, channel_id: str, days: int = 7, limit: int = 500,
    ) -> list[AtomicFact]:
        """Fetch Tier 2 facts from the last N days for a channel."""
        from datetime import timedelta, timezone

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

        def _fetch() -> list[AtomicFact]:
            collection = self._collection()
            weaviate_filter = (
                Filter.by_property("channel_id").equal(channel_id)
                & Filter.by_property("tier").equal("atomic")
                & Filter.by_property("valid_at").greater_or_equal(cutoff)
            )
            result = collection.query.fetch_objects(
                filters=weaviate_filter,
                limit=limit,
            )
            return [self._obj_to_fact(obj) for obj in result.objects]

        return await asyncio.to_thread(_fetch)

    async def batch_update_fact_clusters(
        self, updates: list[tuple[str, str]],
    ) -> None:
        """Batch update cluster_id on multiple facts."""

        def _batch() -> None:
            collection = self._collection()
            for fact_id, cluster_id in updates:
                collection.data.update(
                    uuid=fact_id,
                    properties={"cluster_id": cluster_id},
                )

        await asyncio.to_thread(_batch)

    async def delete_cluster(self, cluster_id: str) -> None:
        """Delete a cluster object by UUID."""

        def _delete() -> None:
            collection = self._collection()
            collection.data.delete_by_id(uuid=cluster_id)

        await asyncio.to_thread(_delete)
