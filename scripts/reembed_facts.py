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
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from beever_atlas.stores import StoreClients

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


_BATCH_SIZE = 100
# Checkpoint after every batch so the UI's progress bar feels responsive.
# Each checkpoint is one tiny Mongo upsert — negligible vs the embed work.
_CHECKPOINT_EVERY = 100
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
    """Drop-and-rebuild the MemoryFact collection with new-dim vectors.

    Weaviate's HNSW index dim is locked at collection-create time, so an
    in-place ``data.update(vector=...)`` errors with HTTP 500 when the
    new vector length differs from the existing one. The only way to
    swap dim is to snapshot rows → drop the collection → recreate it →
    bulk-insert with new vectors and the original UUIDs (so Neo4j-side
    foreign keys remain valid).

    Stages reported through ``_checkpoint`` for UI visibility:
      1. ``weaviate_snapshot``     — read all rows + properties.
      2. ``weaviate_embed``        — re-embed memory_text per batch.
      3. ``weaviate_rebuild``      — drop, recreate, bulk-insert.

    ``resume_from`` is accepted for signature compatibility but ignored:
    a partial rebuild is unsafe (some rows old-dim, some new-dim ⇒
    Weaviate hangs/errors), so the migration always starts fresh. The
    crash-recovery story is "re-run the whole thing"; that's bounded by
    the fact-count, not by where the previous attempt failed.
    """
    from beever_atlas.llm.embeddings import embed_texts

    if resume_from:
        logger.info(
            "reembed: ignoring resume_from=%d — dim-change rebuild is all-or-nothing",
            resume_from,
        )

    # ── Stage 1: snapshot ─────────────────────────────────────────────
    records = await stores.weaviate.snapshot_all_facts_for_reembed()
    total = len(records)
    await _checkpoint(stores, stage="weaviate_snapshot", processed=total, total=total)
    logger.info("reembed: snapshot %d rows from MemoryFact", total)
    if total == 0:
        await _checkpoint(stores, stage="weaviate_facts", processed=0, total=0)
        return 0

    # ── Stage 2: re-embed each row's memory_text ──────────────────────
    await _checkpoint(stores, stage="weaviate_embed", processed=0, total=total)
    sem = asyncio.Semaphore(concurrency)
    embedded = 0
    embedded_lock = asyncio.Lock()

    async def _embed_chunk(start: int) -> None:
        nonlocal embedded
        chunk = records[start : start + _BATCH_SIZE]
        texts = [r["memory_text"] for r in chunk]
        async with sem:
            vectors = await embed_texts(texts)
        # Stitch new vectors back into the in-memory record list under
        # the same indices — the per-chunk slice references the same
        # objects so this updates the master ``records`` list directly.
        for r, vec in zip(chunk, vectors, strict=True):
            r["vector"] = vec
        async with embedded_lock:
            embedded += len(chunk)
            await _checkpoint(
                stores, stage="weaviate_embed", processed=embedded, total=total
            )
            logger.info("reembed: embedded %d/%d weaviate rows", embedded, total)

    embed_tasks = [
        asyncio.create_task(_embed_chunk(i))
        for i in range(0, total, _BATCH_SIZE)
    ]
    await asyncio.gather(*embed_tasks)

    # Sanity check: every record must have a vector before we drop the
    # collection — otherwise we'd lose data.
    missing = [r for r in records if not r.get("vector")]
    if missing:
        raise RuntimeError(
            f"reembed: {len(missing)} of {total} rows are missing a vector "
            f"after the embed pass — refusing to drop the collection"
        )

    # ── Stage 3: drop, recreate, bulk-insert ──────────────────────────
    await _checkpoint(stores, stage="weaviate_rebuild", processed=0, total=total)
    logger.info("reembed: dropping MemoryFact collection (HNSW dim locked)")
    await stores.weaviate.drop_and_recreate_memoryfact()
    logger.info("reembed: bulk-inserting %d rows at new dim", total)
    inserted = await stores.weaviate.bulk_reinsert_with_vectors(records)
    await _checkpoint(
        stores, stage="weaviate_rebuild", processed=inserted, total=total
    )
    logger.info("reembed: weaviate rebuild complete (%d rows reinserted)", inserted)
    # Report final progress under the stage name the existing UI banner
    # already knows about so the percent + ETA logic stays valid.
    await _checkpoint(stores, stage="weaviate_facts", processed=total, total=total)
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

    # Opening checkpoint so the UI flips from ``weaviate_facts → neo4j_names``
    # and shows the new total immediately.
    await _checkpoint(stores, stage="neo4j_names", processed=0, total=total)

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


async def main(
    *,
    resume: bool = False,
    dry_run: bool = False,
    stores: "StoreClients | None" = None,
) -> None:
    """Re-embed all facts + entity name vectors.

    Callable from both CLI (``__main__`` parses argv, then calls this) and
    the FastAPI migration handler (``embedding_settings.start_migration``).
    Does NOT touch ``sys.argv`` — invoking inside uvicorn used to read
    uvicorn's own argv and ``sys.exit(2)`` the whole process.

    Stores ownership:

      * ``stores=None`` (CLI default): we create our own ``StoreClients``,
        call ``init_stores``, run ``startup``, and shut it down in the
        ``finally`` block. Self-contained.
      * ``stores=<existing>`` (API call from inside uvicorn): we use the
        caller's stores (typically the server's global singleton) and do
        NOT call ``startup``/``shutdown`` on it. Previously the migration
        job overwrote the global with its own ``StoreClients`` then closed
        it in ``finally``, leaving the running server unable to talk to
        MongoDB. ``Cannot use MongoClient after close`` errors followed.
    """
    from beever_atlas.infra.config import get_settings
    from beever_atlas.llm.embedding_runtime import (
        reset_migration_context,
        resolve_effective_settings,
        set_migration_context,
    )
    from beever_atlas.llm.embeddings import initialize_embedding_runtime

    env_settings = get_settings()
    initialize_embedding_runtime(env_settings)

    # The re-embed must use *effective* settings (env + DB overrides) so
    # that ``embedding_meta`` at the end of the job reflects what the
    # vectors are actually embedded with — NOT the env baseline. Without
    # this, after a successful migration ``embedding_meta`` would still
    # record the old provider/model/dim and the runtime gate's
    # ``persisted vs effective`` comparison would silently lie.
    settings = await resolve_effective_settings(env_settings)

    own_stores = stores is None
    if own_stores:
        from beever_atlas.stores import StoreClients, init_stores

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

        if dry_run:
            logger.info("reembed: --dry-run, exiting without changes")
            return

        # Resume?
        resume_from_facts = 0
        if resume:
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
        if own_stores:
            assert stores is not None  # narrows for type-checker
            await stores.shutdown()


def _parse_cli_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Re-embed all facts + entity name vectors.")
    parser.add_argument("--resume", action="store_true", help="resume from last checkpoint")
    parser.add_argument("--dry-run", action="store_true", help="report counts, do nothing")
    return parser.parse_args()


if __name__ == "__main__":
    _args = _parse_cli_args()
    asyncio.run(main(resume=_args.resume, dry_run=_args.dry_run))
