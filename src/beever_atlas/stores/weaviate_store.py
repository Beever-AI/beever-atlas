"""Weaviate store client for AtomicFact storage and retrieval."""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime
from typing import Any

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import Filter

from beever_atlas.models import AtomicFact, MemoryFilters, PaginatedFacts

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
            if self._url in ("http://localhost:8080", "http://127.0.0.1:8080", "localhost"):
                return weaviate.connect_to_local()
            headers: dict[str, str] = {}
            if self._api_key:
                headers["X-Weaviate-Api-Key"] = self._api_key
            return weaviate.connect_to_custom(
                http_host=self._url,
                http_port=443,
                http_secure=True,
                grpc_host=self._url,
                grpc_port=50051,
                grpc_secure=True,
                headers=headers,
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
        ("valid_at", DataType.DATE),
        ("invalid_at", DataType.DATE),
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
                Property(name="memory_text", data_type=DataType.TEXT),
                Property(name="quality_score", data_type=DataType.NUMBER),
                Property(name="tier", data_type=DataType.TEXT),
                Property(name="cluster_id", data_type=DataType.TEXT),
                Property(name="channel_id", data_type=DataType.TEXT),
                Property(name="platform", data_type=DataType.TEXT),
                Property(name="author_id", data_type=DataType.TEXT),
                Property(name="author_name", data_type=DataType.TEXT),
                Property(name="message_ts", data_type=DataType.TEXT),
                Property(name="thread_ts", data_type=DataType.TEXT),
                Property(name="source_message_id", data_type=DataType.TEXT),
                Property(name="topic_tags", data_type=DataType.TEXT_ARRAY),
                Property(name="entity_tags", data_type=DataType.TEXT_ARRAY),
                Property(name="action_tags", data_type=DataType.TEXT_ARRAY),
                Property(name="importance", data_type=DataType.TEXT),
                Property(name="graph_entity_ids", data_type=DataType.TEXT_ARRAY),
                Property(name="valid_at", data_type=DataType.DATE),
                Property(name="invalid_at", data_type=DataType.DATE),
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
            "cluster_id": fact.cluster_id or "",
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
        }
        # Weaviate DATE fields require proper datetime objects or must be omitted.
        valid_at = WeaviateStore._coerce_date(fact.valid_at)
        if valid_at is not None:
            props["valid_at"] = valid_at
        invalid_at = WeaviateStore._coerce_date(fact.invalid_at)
        if invalid_at is not None:
            props["invalid_at"] = invalid_at
        return props

    @staticmethod
    def _obj_to_fact(obj: Any) -> AtomicFact:
        """Convert a Weaviate data object back to an AtomicFact."""
        props = obj.properties
        return AtomicFact(
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
            valid_at=props.get("valid_at"),
            invalid_at=props.get("invalid_at"),
        )

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
            weaviate_filter: Any = Filter.by_property("channel_id").equal(channel_id)

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
            weaviate_filter = (
                Filter.by_property("channel_id").equal(channel_id)
                if channel_id
                else None
            )
            result = collection.aggregate.over_all(
                filters=weaviate_filter,
                total_count=True,
            )
            return result.total_count or 0

        return await asyncio.to_thread(_count)

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
