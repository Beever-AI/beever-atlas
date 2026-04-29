"""Data store clients with lifecycle management.

Provides a StoreClients singleton that manages connections to all data stores
(MongoDB, Weaviate, Neo4j) with proper startup/shutdown via FastAPI lifespan.

Issue #36 — `_stores_ready` `asyncio.Event` lets non-HTTP code paths
(background tasks, scheduler jobs, MCP handlers) `await wait_for_stores_ready()`
to tolerate startup races. HTTP requests don't need it because FastAPI's
lifespan ensures `init_stores()` completes before any request fires.
"""

from __future__ import annotations

import asyncio
import logging

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
from beever_atlas.stores.chat_history_store import ChatHistoryStore
from beever_atlas.stores.qa_history_store import QAHistoryStore
from beever_atlas.stores.file_store import FileStore
from beever_atlas.services.share_store import ShareStore
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
        chat_history: ChatHistoryStore,
        qa_history: QAHistoryStore,
        file_store: FileStore,
        share_store: ShareStore,
    ):
        self.mongodb = mongodb
        self.weaviate = weaviate
        self.graph = graph
        self.entity_registry = entity_registry
        self.platform = platform
        self.chat_history = chat_history
        self.qa_history = qa_history
        self.file_store = file_store
        self.share_store = share_store

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

        # The 4 stores below currently each open their own connection pool —
        # the goal of issue #31 is to eliminate per-request store construction
        # in api/ask.py. Phase 1 (this change) just consolidates them into the
        # singleton so subsequent phases can swap callsites to use the shared
        # instances. Each store's internal connection pooling is unchanged for
        # now; pool unification across stores is a separate cleanup.
        chat_history = ChatHistoryStore(settings.mongodb_uri)
        qa_history = QAHistoryStore(settings.weaviate_url, settings.weaviate_api_key)
        file_store = FileStore(settings.mongodb_uri)
        share_store = ShareStore(settings.mongodb_uri)

        return cls(
            mongodb=mongodb,
            weaviate=weaviate,
            graph=graph,
            entity_registry=entity_registry,
            platform=platform,
            chat_history=chat_history,
            qa_history=qa_history,
            file_store=file_store,
            share_store=share_store,
        )

    async def startup(self) -> None:
        await self.mongodb.startup()
        await self.weaviate.startup()
        await self.graph.startup()
        await self.platform.startup()
        await self.chat_history.startup()
        await self.qa_history.startup()
        await self.file_store.startup()
        await self.share_store.startup()

    async def shutdown(self) -> None:
        # Per-store close()/shutdown() — order matches startup() in reverse.
        # The 4 new stores expose either close() (sync) or shutdown() (async);
        # ShareStore, FileStore, ChatHistoryStore use sync close(); QAHistoryStore
        # has async shutdown().
        self.share_store.close()
        self.file_store.close()
        await self.qa_history.shutdown()
        self.chat_history.close()
        await self.graph.shutdown()
        await self.weaviate.shutdown()
        await self.mongodb.shutdown()


logger = logging.getLogger(__name__)

_stores: StoreClients | None = None
_stores_ready: asyncio.Event = asyncio.Event()


def init_stores(stores: StoreClients) -> None:
    """Set the global store clients singleton.

    Re-initialization is allowed (test fixtures rely on this) but logs a
    WARNING — in production it would indicate a bug in the lifespan
    sequencing.
    """
    global _stores
    if _stores is not None:
        logger.warning(
            "init_stores called while _stores is already set; "
            "overwriting singleton (this is normal in tests, "
            "unexpected in production)."
        )
    _stores = stores
    _stores_ready.set()


# NOTE: background tasks / non-HTTP code paths should
# `await wait_for_stores_ready()` before calling `get_stores()` to
# tolerate startup races (issue #36).
def get_stores() -> StoreClients:
    """Return the global store clients. Raises if not initialized."""
    if _stores is None:
        raise RuntimeError("Stores not initialized. Call init_stores() during app startup.")
    return _stores


async def wait_for_stores_ready(timeout: float | None = 30.0) -> None:
    """Block until ``init_stores()`` has been called.

    Use this in background tasks or non-HTTP code paths that may start
    before the FastAPI lifespan has finished initializing stores::

        await wait_for_stores_ready()
        stores = get_stores()  # guaranteed to succeed

    HTTP request handlers do NOT need this — FastAPI's lifespan protocol
    ensures stores are ready before any request is served.

    Args:
        timeout: Seconds to wait. Default 30s — long enough that the
            lifespan has clearly hung, short enough that callers don't
            wait forever in misconfigured environments (CLI entry points
            that bypass the lifespan, tests that import without setup).
            Pass ``None`` to wait indefinitely. On timeout, raises
            ``RuntimeError`` with a diagnostic message rather than
            ``asyncio.TimeoutError`` so the failure mode is obvious.
    """
    if timeout is None:
        await _stores_ready.wait()
        return
    try:
        await asyncio.wait_for(_stores_ready.wait(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Stores not initialized within {timeout}s — is "
            "init_stores() being called via the FastAPI lifespan?"
        ) from exc


def _reset_stores_for_tests() -> None:
    """Reset stores state for test isolation. Not for production use.

    Replaces ``_stores_ready`` with a fresh ``asyncio.Event`` rather than
    calling ``.clear()`` so tests in different event loops don't
    accidentally share state.
    """
    global _stores, _stores_ready
    _stores = None
    _stores_ready = asyncio.Event()
