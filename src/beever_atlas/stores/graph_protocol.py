"""GraphStore protocol — backend-agnostic interface for the knowledge graph.

Any graph database backend (Neo4j, Nebula, etc.) implements this protocol.
Consumers depend on GraphStore, never on a concrete store class.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph


@runtime_checkable
class GraphStore(Protocol):
    """Protocol that every graph-database backend must satisfy.

    All methods are async.  ID values returned or accepted are opaque,
    backend-specific strings that consumers must never parse.
    """

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        """Verify connectivity and prepare the store for use."""
        ...

    async def shutdown(self) -> None:
        """Release connections and clean up resources."""
        ...

    async def ensure_schema(self) -> None:
        """Create indexes, constraints, or schema objects required by the
        backend.  Must be idempotent — safe to call multiple times.

        Neo4j: creates indexes (currently done inside ``startup``).
        Nebula: ``CREATE SPACE / TAG / EDGE TYPE`` with propagation retry.
        """
        ...

    # ------------------------------------------------------------------
    # Entity CRUD
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: GraphEntity) -> str:
        """Insert or update an entity node.  Returns an opaque backend ID."""
        ...

    async def batch_upsert_entities(self, entities: list[GraphEntity]) -> list[str]:
        """Upsert multiple entities.  Returns a list of opaque backend IDs."""
        ...

    async def get_entity(self, entity_id: str) -> GraphEntity | None:
        """Return an entity by its opaque backend-specific ID, or ``None``.

        Use this to resolve stored references such as ``graph_entity_ids``
        persisted in Weaviate facts.  For lookup-by-name prefer
        :meth:`find_entity_by_name`.
        """
        ...

    async def find_entity_by_name(self, name: str) -> GraphEntity | None:
        """Return an entity by its business-key *name*, or ``None``.

        Preferred for consumer lookups.  Returns the entity regardless of
        backend ID format.
        """
        ...

    async def list_entities(
        self,
        channel_id: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
        include_pending: bool = False,
    ) -> list[GraphEntity]:
        """Return entities with optional channel / type / pending filters."""
        ...

    async def count_entities(self, channel_id: str | None = None) -> int:
        """Return the total entity count, optionally scoped to a channel."""
        ...

    async def promote_pending_entity(self, entity_name: str) -> None:
        """Promote a pending entity to active status."""
        ...

    async def prune_expired_pending(self, grace_period_days: int = 7) -> int:
        """Delete pending entities older than *grace_period_days*.

        Returns the number of pruned entities.
        """
        ...

    # ------------------------------------------------------------------
    # Relationship CRUD
    # ------------------------------------------------------------------

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        """Insert or update a relationship.  Returns an opaque backend ID."""
        ...

    async def batch_upsert_relationships(self, rels: list[GraphRelationship]) -> list[str]:
        """Upsert multiple relationships.  Returns opaque backend IDs."""
        ...

    async def list_relationships(
        self, channel_id: str | None = None, limit: int = 200
    ) -> list[GraphRelationship]:
        """Return relationships, optionally scoped to a channel."""
        ...

    async def count_relationships(self, channel_id: str | None = None) -> int:
        """Return the total relationship count, optionally scoped."""
        ...

    # ------------------------------------------------------------------
    # Episodic + Media
    # ------------------------------------------------------------------

    async def create_episodic_link(
        self,
        entity_name: str,
        weaviate_fact_id: str,
        message_ts: str,
        channel_id: str = "",
        media_urls: list[str] | None = None,
        link_urls: list[str] | None = None,
    ) -> None:
        """Link an entity to an episodic Event node."""
        ...

    async def upsert_media(
        self,
        url: str,
        media_type: str,
        title: str = "",
        channel_id: str = "",
        message_ts: str = "",
    ) -> None:
        """Insert or update a Media node by URL.  Idempotent."""
        ...

    async def link_entity_to_media(self, entity_name: str, media_url: str) -> None:
        """Create a REFERENCES_MEDIA relationship from entity to media."""
        ...

    async def list_media(
        self, channel_id: str | None = None, limit: int = 50
    ) -> list[dict[str, Any]]:
        """Return Media nodes, optionally filtered by channel."""
        ...

    async def list_media_relationships(
        self, channel_id: str | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        """Return REFERENCES_MEDIA relationships between entities and media."""
        ...

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    async def get_neighbors(self, entity_id: str, hops: int = 1, limit: int = 50) -> Subgraph:
        """Return the neighborhood subgraph up to *hops* from an entity."""
        ...

    async def get_decisions(self, channel_id: str, limit: int = 20) -> list[GraphEntity]:
        """Return entities of type 'Decision' visible in a channel."""
        ...

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_channel_data(self, channel_id: str) -> dict[str, int]:
        """Delete all entities, events, media, and relationships for a
        channel.  Returns counts of deleted items."""
        ...

    # ------------------------------------------------------------------
    # Entity-registry support
    # (replaces raw execute_query leaks in EntityRegistry / Persister)
    # ------------------------------------------------------------------

    async def find_entity_by_name_or_alias(self, name: str) -> str | None:
        """Find an entity by exact name **or** alias.

        Returns the canonical (node) name, or ``None`` if no match.
        """
        ...

    async def get_all_entities_summary(self) -> list[dict[str, Any]]:
        """Return all entities as lightweight dicts with ``name``, ``type``,
        and ``aliases`` keys.  Used for pipeline state injection."""
        ...

    async def register_alias(self, canonical: str, alias: str, entity_type: str) -> None:
        """Append *alias* to the aliases list of the entity named
        *canonical*.  No-op if the entity does not exist."""
        ...

    async def fuzzy_match_entities(
        self, name: str, threshold: float = 0.8
    ) -> list[tuple[str, float]]:
        """Return ``(canonical_name, score)`` pairs for entities whose name
        is similar to *name* (Jaro-Winkler, score >= *threshold*).

        Implementations should use ``jellyfish.jaro_winkler_similarity`` as
        the portable reference algorithm.  Neo4j may additionally use
        ``apoc.text.jaroWinklerDistance`` as an internal optimisation.
        """
        ...

    async def get_entities_with_name_vectors(self) -> list[dict[str, Any]]:
        """Return dicts with ``name`` and ``vec`` for entities that have a
        stored ``name_vector``."""
        ...

    async def get_entities_missing_name_vectors(self) -> list[str]:
        """Return entity names that do **not** have a ``name_vector``."""
        ...

    async def store_name_vector(self, entity_name: str, vector: list[float]) -> None:
        """Persist a pre-computed name-embedding vector on an entity node."""
        ...

    # ------------------------------------------------------------------
    # Batch operations (optimised for persister pipeline)
    # ------------------------------------------------------------------

    async def batch_create_episodic_links(self, links: list[dict[str, Any]]) -> int:
        """Create multiple episodic links in one batch operation.

        Each link dict has: entity_name, weaviate_fact_id, message_ts,
        channel_id, media_urls, link_urls.
        Returns count of links created.
        """
        ...

    async def batch_upsert_media(self, items: list[dict[str, Any]]) -> int:
        """Batch upsert media nodes.

        Each item has: url, media_type, title, channel_id, message_ts.
        Returns count of media nodes upserted.
        """
        ...

    async def batch_link_entities_to_media(self, links: list[dict[str, Any]]) -> int:
        """Batch create entity-to-media links.

        Each link has: entity_name, media_url.
        Returns count of links created.
        """
        ...

    async def batch_promote_pending(self, names: list[str]) -> int:
        """Batch promote pending entities to active.  Returns count promoted."""
        ...

    async def batch_find_entities_by_name(self, names: list[str]) -> set[str]:
        """Check which entity names exist in the graph.

        Returns set of existing names.
        """
        ...
