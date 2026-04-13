"""Data store clients with lifecycle management.

Provides a StoreClients singleton that manages connections to all data stores
(MongoDB, Weaviate, Neo4j) with proper startup/shutdown via FastAPI lifespan.
"""

from __future__ import annotations

from beever_atlas.stores.mongodb_store import MongoDBStore
from beever_atlas.stores.weaviate_store import WeaviateStore
from beever_atlas.stores.neo4j_store import Neo4jStore
from beever_atlas.stores.graph_protocol import GraphStore
from beever_atlas.stores.graph_errors import (
    GraphBackendUnavailable as GraphBackendUnavailable,
    GraphConflict as GraphConflict,
    GraphNotFound as GraphNotFound,
    GraphStoreError as GraphStoreError,
)
from beever_atlas.stores.entity_registry import EntityRegistry
from beever_atlas.stores.platform_store import PlatformStore
from beever_atlas.infra.config import Settings


class StoreClients:
    """Manages all data store connections with lifecycle hooks."""

    def __init__(
        self,
        mongodb: MongoDBStore,
        weaviate: WeaviateStore,
        graph: GraphStore,
        entity_registry: EntityRegistry,
        platform: PlatformStore,
    ):
        self.mongodb = mongodb
        self.weaviate = weaviate
        self.graph = graph
        self.entity_registry = entity_registry
        self.platform = platform

    @classmethod
    def from_settings(cls, settings: Settings) -> StoreClients:
        mongodb = MongoDBStore(settings.mongodb_uri)
        weaviate = WeaviateStore(settings.weaviate_url, settings.weaviate_api_key)

        if settings.graph_backend == "neo4j":
            graph: GraphStore = Neo4jStore(
                settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password
            )
        elif settings.graph_backend == "nebula":
            from beever_atlas.stores.nebula_store import NebulaStore

            graph = NebulaStore(
                settings.nebula_hosts,
                settings.nebula_user,
                settings.nebula_password,
                settings.nebula_space,
            )
        elif settings.graph_backend == "none":
            from beever_atlas.stores.null_graph import NullGraphStore

            graph = NullGraphStore()
        else:
            raise ValueError(
                f"Unknown graph backend: {settings.graph_backend!r}. "
                "Expected 'neo4j', 'nebula', or 'none'."
            )

        entity_registry = EntityRegistry(graph)
        # Reuse the same MongoDB connection as MongoDBStore
        platform = PlatformStore(mongodb.db["platform_connections"])
        return cls(
            mongodb=mongodb,
            weaviate=weaviate,
            graph=graph,
            entity_registry=entity_registry,
            platform=platform,
        )

    async def startup(self) -> None:
        await self.mongodb.startup()
        await self.weaviate.startup()
        await self.graph.startup()
        await self.platform.startup()

    async def shutdown(self) -> None:
        await self.graph.shutdown()
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
