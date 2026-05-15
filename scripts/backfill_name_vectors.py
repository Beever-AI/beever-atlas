"""Backfill: compute and store name_vector embeddings for existing entities.

Usage:
    python -m scripts.backfill_name_vectors

Queries all Entity nodes in Neo4j that lack a name_vector property, computes
embeddings via the provider-agnostic shim, and stores the vectors. The shim
handles chunking (100 / batch), retries, and rate limiting, so this script
is now a thin orchestrator.
"""

from __future__ import annotations

import asyncio
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


async def main() -> None:
    from beever_atlas.infra.config import get_settings
    from beever_atlas.llm.embeddings import embed_texts, initialize_embedding_runtime
    from beever_atlas.stores import StoreClients, init_stores

    settings = get_settings()
    initialize_embedding_runtime(settings)

    stores = StoreClients.from_settings(settings)
    init_stores(stores)
    await stores.startup()

    try:
        names = await stores.graph.get_entities_missing_name_vectors()
        logger.info("Found %d entities without name_vector", len(names))

        if not names:
            logger.info("Nothing to backfill.")
            return

        # Hand the full list to the shim — it does chunking, retry, and the
        # rate-limit acquisition. We slice the result in 100-name groups so
        # the operator log line stays informative on long runs.
        vectors = await embed_texts(names)

        for i in range(0, len(names), 100):
            slice_names = names[i : i + 100]
            slice_vectors = vectors[i : i + 100]
            for name, vector in zip(slice_names, slice_vectors, strict=True):
                await stores.entity_registry.store_name_vector(name, vector)
            logger.info(
                "Backfilled %d/%d entities", min(i + 100, len(names)), len(names)
            )

        logger.info("Backfill complete: %d/%d entities", len(names), len(names))

    finally:
        await stores.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
