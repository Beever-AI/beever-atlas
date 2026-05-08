"""Re-embed every Weaviate fact + Neo4j entity name vector under the current
embedding model (PR-C, ``make reembed-all``).

Usage:
    python -m scripts.reembed_facts            # full pass
    python -m scripts.reembed_facts --resume   # resume from last checkpoint
    python -m scripts.reembed_facts --dry-run  # report counts, do nothing

Process:
  1. Walk every ``AtomicFact`` in Weaviate, group by 100, compute embeddings
     via the configured provider, ``data.update(uuid=..., vector=...)``.
  2. Walk every ``Entity.name_vector`` in Neo4j, group by 100, compute
     embeddings, store via the entity registry.
  3. Once both stores succeed, atomically update ``embedding_meta`` in
     MongoDB so the next boot's dim guard accepts the new dimension.

Resumability:
  Checkpoints land in MongoDB collection ``reembed_state`` every 500 rows.
  ``--resume`` reads the checkpoint and continues from there.

Concurrency:
  Bounded by ``EMBEDDING_REEMBED_CONCURRENCY`` (default 4). The shared
  ``EMBEDDING_LIMITER`` still applies, so the effective rate is min(
  concurrency × 100 / round-trip, EMBEDDING_RPM).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import UTC, datetime

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


_BATCH_SIZE = 100
_CHECKPOINT_EVERY = 500
_REEMBED_STATE_DOC_ID = "reembed_state"


async def _checkpoint(stores, *, stage: str, processed: int, total: int) -> None:
    await stores.mongodb.db["reembed_state"].update_one(
        {"_id": _REEMBED_STATE_DOC_ID},
        {
            "$set": {
                "stage": stage,
                "processed": processed,
                "total": total,
                "updated_at": datetime.now(tz=UTC).isoformat(),
            }
        },
        upsert=True,
    )


async def _load_checkpoint(stores) -> dict | None:
    doc = await stores.mongodb.db["reembed_state"].find_one({"_id": _REEMBED_STATE_DOC_ID})
    if doc:
        doc.pop("_id", None)
    return doc


async def _clear_checkpoint(stores) -> None:
    await stores.mongodb.db["reembed_state"].delete_one({"_id": _REEMBED_STATE_DOC_ID})


async def _reembed_weaviate_facts(stores, *, concurrency: int, resume_from: int = 0) -> int:
    """Re-embed every atomic fact's vector. Returns the number processed."""
    from beever_atlas.llm.embeddings import embed_texts

    rows = await stores.weaviate.iter_atomic_fact_ids_and_text()
    total = len(rows)
    if resume_from > 0:
        rows = rows[resume_from:]
        logger.info(
            "reembed: resuming from row %d (skipping %d already done)", resume_from, resume_from
        )

    sem = asyncio.Semaphore(concurrency)

    async def _process_chunk(chunk_rows: list[tuple[str, str]]) -> None:
        async with sem:
            uuids, texts = zip(*chunk_rows)
            vectors = await embed_texts(list(texts))
            for uuid, vec in zip(uuids, vectors, strict=True):
                await stores.weaviate.update_fact_vector(uuid, vec)

    processed = resume_from
    pending: list[asyncio.Task] = []
    for i in range(0, len(rows), _BATCH_SIZE):
        chunk = rows[i : i + _BATCH_SIZE]
        pending.append(asyncio.create_task(_process_chunk(chunk)))
        processed += len(chunk)

        # Checkpoint + log every _CHECKPOINT_EVERY rows so a crash loses at
        # most that many rows of work.
        if processed % _CHECKPOINT_EVERY < _BATCH_SIZE:
            # Wait for the in-flight wave so the checkpoint reflects only
            # rows actually written.
            await asyncio.gather(*pending)
            pending = []
            await _checkpoint(stores, stage="weaviate_facts", processed=processed, total=total)
            logger.info("reembed: weaviate facts %d/%d", processed, total)

    if pending:
        await asyncio.gather(*pending)
    await _checkpoint(stores, stage="weaviate_facts", processed=total, total=total)
    logger.info("reembed: weaviate facts complete (%d rows)", total)
    return total


async def _reembed_neo4j_name_vectors(stores) -> int:
    """Re-embed every entity name vector. Returns the number processed."""
    from beever_atlas.llm.embeddings import embed_texts

    # ``get_entities_with_name_vectors`` returns the rows that already have
    # a vector — those are the ones that need replacing under the new
    # model. (Entities missing a name_vector are picked up by the existing
    # ``backfill_name_vectors`` script, which now uses the same shim.)
    records = await stores.graph.get_entities_with_name_vectors()
    names = [r["name"] for r in records if r.get("name")]
    total = len(names)
    if not total:
        return 0

    for i in range(0, total, _BATCH_SIZE):
        chunk = names[i : i + _BATCH_SIZE]
        vectors = await embed_texts(chunk)
        for name, vec in zip(chunk, vectors, strict=True):
            await stores.entity_registry.store_name_vector(name, vec)
        if (i + _BATCH_SIZE) % _CHECKPOINT_EVERY < _BATCH_SIZE:
            await _checkpoint(
                stores,
                stage="neo4j_names",
                processed=min(i + _BATCH_SIZE, total),
                total=total,
            )
            logger.info("reembed: neo4j name_vectors %d/%d", min(i + _BATCH_SIZE, total), total)
    await _checkpoint(stores, stage="neo4j_names", processed=total, total=total)
    logger.info("reembed: neo4j name_vectors complete (%d rows)", total)
    return total


async def main() -> None:
    parser = argparse.ArgumentParser(description="Re-embed all facts + entity name vectors.")
    parser.add_argument("--resume", action="store_true", help="resume from last checkpoint")
    parser.add_argument("--dry-run", action="store_true", help="report counts, do nothing")
    args = parser.parse_args()

    from beever_atlas.infra.config import get_settings
    from beever_atlas.llm.embedding_runtime import (
        reset_migration_context,
        set_migration_context,
    )
    from beever_atlas.llm.embeddings import initialize_embedding_runtime
    from beever_atlas.stores import StoreClients, init_stores

    settings = get_settings()
    initialize_embedding_runtime(settings)

    stores = StoreClients.from_settings(settings)
    init_stores(stores)
    await stores.startup()

    # Mark this asyncio task as the migration job so its own embed_texts
    # calls bypass the migration-mode gate (which would otherwise refuse
    # to embed during the very migration that's about to run).
    _migration_token = set_migration_context(True)

    try:
        # Snapshot current counts upfront so the cost-preview line is
        # accurate even if rows arrive mid-run.
        weaviate_count = await stores.weaviate.count_facts()
        neo4j_records = await stores.graph.get_entities_with_name_vectors()
        neo4j_count = len(neo4j_records)
        del neo4j_records  # release before the long pass

        logger.info(
            "reembed: provider=%s model=%s dim=%d facts=%d names=%d concurrency=%d",
            settings.embedding_provider,
            settings.embedding_model,
            settings.embedding_dimensions,
            weaviate_count,
            neo4j_count,
            settings.embedding_reembed_concurrency,
        )

        if args.dry_run:
            logger.info("reembed: --dry-run, exiting without changes")
            return

        # Resume?
        resume_from_facts = 0
        if args.resume:
            cp = await _load_checkpoint(stores)
            if cp and cp.get("stage") == "weaviate_facts":
                resume_from_facts = int(cp.get("processed") or 0)

        await _reembed_weaviate_facts(
            stores,
            concurrency=settings.embedding_reembed_concurrency,
            resume_from=resume_from_facts,
        )
        await _reembed_neo4j_name_vectors(stores)

        # Atomic final flip: only NOW does ``embedding_meta`` reflect the
        # new dimension so a crash before this point keeps the dim guard
        # consistent with what's actually in storage.
        await stores.mongodb.set_embedding_meta(
            provider=settings.embedding_provider,
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
            ok=True,
            error=None,
        )
        await _clear_checkpoint(stores)
        logger.info(
            "reembed: complete. embedding_meta now reflects %s/%s @ %d",
            settings.embedding_provider,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
    finally:
        reset_migration_context(_migration_token)
        await stores.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
