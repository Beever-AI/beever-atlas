"""Null graph store — no-op implementation of the GraphStore protocol.

Used when ``GRAPH_BACKEND=none`` to allow the application to run without
any graph database connection.  Every method returns an empty/default value.
"""

from __future__ import annotations

from typing import Any

from beever_atlas.models import GraphEntity, GraphRelationship, Subgraph


class NullGraphStore:
    """No-op GraphStore that satisfies the protocol with empty returns."""

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def startup(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    async def ensure_schema(self) -> None:
        pass

    # ------------------------------------------------------------------
    # Entity CRUD
    # ------------------------------------------------------------------

    async def upsert_entity(self, entity: GraphEntity) -> str:
        return ""

    async def batch_upsert_entities(self, entities: list[GraphEntity]) -> list[str]:
        return []

    async def get_entity(self, entity_id: str) -> GraphEntity | None:
        return None

    async def find_entity_by_name(self, name: str) -> GraphEntity | None:
        return None

    async def list_entities(
        self,
        channel_id: str | None = None,
        entity_type: str | None = None,
        limit: int = 50,
        include_pending: bool = False,
    ) -> list[GraphEntity]:
        return []

    async def count_entities(self, channel_id: str | None = None) -> int:
        return 0

    async def promote_pending_entity(self, entity_name: str) -> None:
        pass

    async def prune_expired_pending(self, grace_period_days: int = 7) -> int:
        return 0

    # ------------------------------------------------------------------
    # Relationship CRUD
    # ------------------------------------------------------------------

    async def upsert_relationship(self, rel: GraphRelationship) -> str:
        return ""

    async def batch_upsert_relationships(
        self,
        rels: list[GraphRelationship],
    ) -> list[str]:
        return []

    async def list_relationships(
        self,
        channel_id: str | None = None,
        limit: int = 200,
    ) -> list[GraphRelationship]:
        return []

    async def count_relationships(self, channel_id: str | None = None) -> int:
        return 0

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
        pass

    async def upsert_media(
        self,
        url: str,
        media_type: str,
        title: str = "",
        channel_id: str = "",
        message_ts: str = "",
    ) -> None:
        pass

    async def link_entity_to_media(self, entity_name: str, media_url: str) -> None:
        pass

    async def list_media(
        self,
        channel_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        return []

    async def list_media_relationships(
        self,
        channel_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return []

    # ------------------------------------------------------------------
    # Traversal
    # ------------------------------------------------------------------

    async def get_neighbors(
        self,
        entity_id: str,
        hops: int = 1,
        limit: int = 50,
    ) -> Subgraph:
        return Subgraph()

    async def get_decisions(
        self,
        channel_id: str,
        limit: int = 20,
    ) -> list[GraphEntity]:
        return []

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    async def delete_channel_data(self, channel_id: str) -> dict[str, int]:
        return {"entities": 0, "relationships": 0, "events": 0, "media": 0}

    # ------------------------------------------------------------------
    # Entity-registry support
    # ------------------------------------------------------------------

    async def find_entity_by_name_or_alias(self, name: str) -> str | None:
        return None

    async def get_all_entities_summary(self) -> list[dict[str, Any]]:
        return []

    async def register_alias(
        self,
        canonical: str,
        alias: str,
        entity_type: str,
    ) -> None:
        pass

    async def fuzzy_match_entities(
        self,
        name: str,
        threshold: float = 0.8,
    ) -> list[tuple[str, float]]:
        return []

    async def get_entities_with_name_vectors(self) -> list[dict[str, Any]]:
        return []

    async def get_entities_missing_name_vectors(self) -> list[str]:
        return []

    async def store_name_vector(
        self,
        entity_name: str,
        vector: list[float],
    ) -> None:
        pass

    # ------------------------------------------------------------------
    # Batch operations
    # ------------------------------------------------------------------

    async def batch_create_episodic_links(self, links: list[dict]) -> int:
        return 0

    async def batch_upsert_media(self, items: list[dict]) -> int:
        return 0

    async def batch_link_entities_to_media(self, links: list[dict]) -> int:
        return 0

    async def batch_promote_pending(self, names: list[str]) -> int:
        return 0

    async def batch_find_entities_by_name(self, names: list[str]) -> set[str]:
        return set()
