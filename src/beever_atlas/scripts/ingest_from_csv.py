"""Ingest a Discord CSV export into the full pipeline (Weaviate + Neo4j + MongoDB).

Reads from the cache file produced by import_discord_csv.py and runs the same
pipeline as SyncRunner._run_sync, so results appear in the dashboard and wiki
generation works normally.

Usage:
    # Step 1 — import CSV to cache (if not done yet):
    uv run python -m beever_atlas.scripts.import_discord_csv <path_to_csv>

    # Step 2 — ingest into the real databases:
    uv run python -m beever_atlas.scripts.ingest_from_csv 440061296017408010

    # Options:
    uv run python -m beever_atlas.scripts.ingest_from_csv 440061296017408010 --limit 500
    uv run python -m beever_atlas.scripts.ingest_from_csv 440061296017408010 --batch-api
    uv run python -m beever_atlas.scripts.ingest_from_csv 440061296017408010 --channel-name "漢藏語"
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")

logger = logging.getLogger(__name__)


def _load_messages(channel_id: str, limit: int = 0):
    """Load NormalizedMessage objects from the cache file."""
    from beever_atlas.adapters.base import NormalizedMessage

    cache_file = Path(".omc/cache") / f"messages-{channel_id}.json"
    if not cache_file.exists():
        raise FileNotFoundError(
            f"Cache file not found: {cache_file}\n"
            f"Run first: uv run python -m beever_atlas.scripts.import_discord_csv <csv_path>"
        )

    raw = json.loads(cache_file.read_text())
    if limit:
        raw = raw[:limit]

    messages = []
    for r in raw:
        ts_raw = r.get("timestamp") or r.get("ts")
        if isinstance(ts_raw, (int, float)):
            ts = datetime.fromtimestamp(ts_raw, tz=timezone.utc)
        elif isinstance(ts_raw, str):
            try:
                ts = datetime.fromisoformat(ts_raw)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except ValueError:
                ts = datetime.now(timezone.utc)
        else:
            ts = datetime.now(timezone.utc)

        messages.append(
            NormalizedMessage(
                content=r.get("content", ""),
                author=r.get("author", ""),
                author_name=r.get("author_name", ""),
                author_image=r.get("author_image", ""),
                platform=r.get("platform", "discord"),
                channel_id=r.get("channel_id", channel_id),
                channel_name=r.get("channel_name", channel_id),
                message_id=r.get("message_id", ""),
                timestamp=ts,
                thread_id=r.get("thread_id"),
                attachments=r.get("attachments", []),
                reactions=r.get("reactions", []),
                reply_count=r.get("reply_count", 0),
                raw_metadata=r.get("raw_metadata", {}),
            )
        )

    return messages


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Ingest Discord CSV cache into the full pipeline")
    parser.add_argument("channel_id", help="Channel ID (matches cache filename)")
    parser.add_argument(
        "--channel-name", default="", help="Human-readable channel name for the dashboard"
    )
    parser.add_argument("--limit", type=int, default=0, help="Max messages to ingest (0 = all)")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=0,
        help="Messages per LLM batch (0 = use server default). Lower = less truncation risk.",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=0,
        help="Max prompt tokens per adaptive batch (0 = use server default, default=6000 → ~120 msgs). Use 1500 for ~30 msgs to avoid entity truncation.",
    )
    parser.add_argument("--batch-api", action="store_true", help="Use Gemini Batch API path")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    from beever_atlas.infra.config import get_settings
    from beever_atlas.llm.provider import init_llm_provider
    from beever_atlas.stores import StoreClients, init_stores
    from beever_atlas.services.batch_processor import BatchProcessor
    from beever_atlas.services.policy_resolver import resolve_effective_policy

    settings = get_settings()
    if args.max_tokens > 0:
        settings.batch_max_prompt_tokens = args.max_tokens
    init_llm_provider(settings)
    stores = StoreClients.from_settings(settings)
    await stores.startup()
    init_stores(stores)

    channel_id = args.channel_id
    channel_name = args.channel_name or channel_id

    # Load messages from cache
    print(f"\nLoading messages from cache for channel {channel_id}...")
    messages = _load_messages(channel_id, limit=args.limit)
    print(f"Loaded {len(messages)} messages")

    if not messages:
        print("No messages to ingest. Exiting.")
        return

    # Create MongoDB sync job
    job = await stores.mongodb.create_sync_job(
        channel_id=channel_id,
        sync_type="full",
        total_messages=len(messages),
        parent_messages=len(messages),
        batch_size=settings.sync_batch_size,
    )
    job_id = job.id
    print(f"Created sync job: {job_id}")
    print(f"Running ingestion pipeline ({len(messages)} messages)...\n")

    # Run the batch processor (same as SyncRunner._run_sync)
    from beever_atlas.models.sync_policy import IngestionConfig

    effective_policy = await resolve_effective_policy(channel_id)
    ingestion_config = effective_policy.ingestion
    if args.batch_size > 0:
        ingestion_config = IngestionConfig(
            batch_size=args.batch_size,
            quality_threshold=ingestion_config.quality_threshold,
            max_facts_per_message=ingestion_config.max_facts_per_message,
        )
    batch_processor = BatchProcessor()

    try:
        result = await batch_processor.process_messages(
            messages=messages,
            channel_id=channel_id,
            channel_name=channel_name,
            sync_job_id=job_id,
            ingestion_config=ingestion_config,
            use_batch_api=args.batch_api,
        )
    except Exception as exc:
        logger.error("Pipeline failed: %s", exc, exc_info=True)
        await stores.mongodb.complete_sync_job(
            job_id=job_id,
            status="failed",
            errors=[str(exc)],
            failed_stage=f"Pipeline error: {str(exc)[:200]}",
        )
        print(f"\nFailed: {exc}")
        return

    # Determine last sync timestamp
    last_ts = None
    timestamps = [m.timestamp for m in messages if m.timestamp and not m.thread_id]
    if timestamps:
        last_ts = max(timestamps).isoformat()

    # Mark job complete
    sync_status = "failed" if result.errors else "completed"
    sync_errors = None
    if result.errors:
        sync_errors = [
            f"batch={err.get('batch_num')} error={err.get('error')}" for err in result.errors
        ]
    await stores.mongodb.complete_sync_job(
        job_id=job_id,
        status=sync_status,
        errors=sync_errors,
    )

    # Update sync state cursor — write even on partial failure so channel
    # appears in sidebar and wiki can be generated from successfully ingested batches.
    if last_ts:
        await stores.mongodb.update_channel_sync_state(
            channel_id=channel_id,
            last_sync_ts=last_ts,
            set_total=len(messages),
        )

    # Log activity
    await stores.mongodb.log_activity(
        event_type="sync_failed" if result.errors else "sync_completed",
        channel_id=channel_id,
        details={
            "job_id": job_id,
            "channel_name": channel_name,
            "total_facts": result.total_facts,
            "total_entities": result.total_entities,
            "total_relationships": result.total_relationships,
            "total_messages": len(messages),
            "error_count": len(result.errors),
            "source": "csv_import",
        },
    )

    print(f"\n{'=' * 60}")
    print("  INGESTION COMPLETE")
    print(f"{'=' * 60}")
    print(f"  Status:        {sync_status}")
    print(f"  Messages:      {len(messages)}")
    print(f"  Facts:         {result.total_facts}")
    print(f"  Entities:      {result.total_entities}")
    print(f"  Relationships: {result.total_relationships}")
    print(f"  Errors:        {len(result.errors)}")
    print()

    if result.errors:
        print("  Errors:")
        for err in result.errors:
            print(f"    - batch={err.get('batch_num')}: {err.get('error')}")
        print()
        return

    # Trigger consolidation → this is what makes wiki generation possible
    print("Triggering consolidation (generates channel summary + clusters for wiki)...")
    from beever_atlas.services.pipeline_orchestrator import on_ingestion_complete

    await on_ingestion_complete(channel_id, result.total_facts)
    print("Consolidation triggered.")
    print()
    print(f"  Channel '{channel_name}' ({channel_id}) is now available in the dashboard.")
    print("  Open the Wiki tab on that channel to generate the wiki.")
    print()


if __name__ == "__main__":
    asyncio.run(main())
