"""Data store clients with lifecycle management.

Provides a StoreClients singleton that manages connections to all data stores
(MongoDB, Weaviate, Neo4j) with proper startup/shutdown via FastAPI lifespan.
"""

from __future__ import annotations

from beever_atlas.stores.mongodb_store import MongoDBStore
from beever_atlas.stores.weaviate_store import WeaviateStore
from beever_atlas.stores.neo4j_store import Neo4jStore
from beever_atlas.stores.entity_registry import EntityRegistry
from beever_atlas.infra.config import Settings


class StoreClients:
    """Manages all data store connections with lifecycle hooks."""

    def __init__(
        self,
        mongodb: MongoDBStore,
        weaviate: WeaviateStore,
        neo4j: Neo4jStore,
        entity_registry: EntityRegistry,
    ):
        self.mongodb = mongodb
        self.weaviate = weaviate
        self.neo4j = neo4j
        self.entity_registry = entity_registry

    @classmethod
    def from_settings(cls, settings: Settings) -> StoreClients:
        mongodb = MongoDBStore(settings.mongodb_uri)
        weaviate = WeaviateStore(settings.weaviate_url, settings.weaviate_api_key)
        neo4j = Neo4jStore(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
        entity_registry = EntityRegistry(neo4j)
        return cls(
            mongodb=mongodb,
            weaviate=weaviate,
            neo4j=neo4j,
            entity_registry=entity_registry,
        )

    async def startup(self) -> None:
        await self.mongodb.startup()
        await self.weaviate.startup()
        await self.neo4j.startup()

    async def shutdown(self) -> None:
        await self.neo4j.shutdown()
        await self.weaviate.shutdown()
        await self.mongodb.shutdown()


_stores: StoreClients | None = None


def init_stores(stores: StoreClients) -> None:
    """Set the global store clients singleton."""
    global _stores
    _stores = stores


def get_stores() -> StoreClients:
    """Return the global store clients. Raises if not initialized."""
    if _stores is None:
        raise RuntimeError("Stores not initialized. Call init_stores() during app startup.")
    return _stores
