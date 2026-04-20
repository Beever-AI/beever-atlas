"""Dry-run extraction — test prompts without touching any database.

Usage:
    # Run against live channel messages (fetches from bridge, caches locally):
    uv run python -m beever_atlas.scripts.dry_run C0AMY9QSPB2

    # Run with cached messages (skip bridge fetch, instant):
    uv run python -m beever_atlas.scripts.dry_run C0AMY9QSPB2 --cached

    # Run only fact extraction (skip entities):
    uv run python -m beever_atlas.scripts.dry_run C0AMY9QSPB2 --facts-only

    # Run only entity extraction (skip facts):
    uv run python -m beever_atlas.scripts.dry_run C0AMY9QSPB2 --entities-only

    # Limit to N messages:
    uv run python -m beever_atlas.scripts.dry_run C0AMY9QSPB2 --limit 3

    # Simulate Batch API path (token estimates + request counts, no DB writes):
    uv run python -m beever_atlas.scripts.dry_run C0AMY9QSPB2 --batch-api

Results are printed as formatted JSON. No Weaviate, Neo4j, or MongoDB writes.
Cached messages are stored in .omc/cache/ for instant re-runs.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

# Load .env before any other imports
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[3] / ".env")


async def _run_batch_api_dry_run(args: Any, settings: Any) -> None:
    """Simulate the Batch API path: show token estimates and request counts."""
    from beever_atlas.agents.prompts.entity_extractor import ENTITY_EXTRACTOR_INSTRUCTION
    from beever_atlas.agents.prompts.fact_extractor import FACT_EXTRACTOR_INSTRUCTION
    from beever_atlas.services.adaptive_batcher import estimate_message_tokens

    cache_dir = Path(".omc/cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"messages-{args.channel_id}.json"

    if args.cached and cache_file.exists():
        print(f"\n📦 Using cached messages from {cache_file}")
        raw_messages = json.loads(cache_file.read_text())
    else:
        print(f"\n🔄 Fetching messages from bridge for channel {args.channel_id}...")
        from beever_atlas.adapters.bridge import ChatBridgeAdapter

        adapter = ChatBridgeAdapter(bridge_url=settings.bridge_url, api_key=settings.bridge_api_key)
        messages = await adapter.fetch_history(args.channel_id, limit=500)
        raw_messages = [vars(m) for m in messages]
        cache_file.write_text(json.dumps(raw_messages, default=str, indent=2))
        print(f"   Cached {len(raw_messages)} messages to {cache_file}")

    if args.limit > 0:
        raw_messages = raw_messages[: args.limit]

    from beever_atlas.services.adaptive_batcher import token_aware_batches

    batches = token_aware_batches(
        raw_messages,
        max_tokens=settings.batch_max_prompt_tokens,
        time_window_seconds=settings.batch_time_window_seconds,
    )

    print(f"\n{'=' * 60}")
    print("  BATCH API DRY-RUN ESTIMATE")
    print(f"{'=' * 60}")
    print(f"  Total messages:  {len(raw_messages)}")
    print(f"  Token budget:    {settings.batch_max_prompt_tokens} tokens/batch")
    print(f"  Total batches:   {len(batches)}  (adaptive, token-aware)")
    print(f"  Batch API calls: {len(batches) * 2}  (fact + entity per batch)\n")

    total_tokens = 0
    for batch_num, batch in enumerate(batches, 1):
        batch_tokens = sum(estimate_message_tokens(m) for m in batch)
        total_tokens += batch_tokens

        # Build prompt approximations to estimate full prompt token cost
        msgs_json = json.dumps(batch, default=str)
        fact_prompt = FACT_EXTRACTOR_INSTRUCTION.format(
            channel_name=args.channel_id,
            preprocessed_messages=msgs_json,
            max_facts_per_message=settings.max_facts_per_message,
        )
        entity_prompt = ENTITY_EXTRACTOR_INSTRUCTION.format(
            channel_name=args.channel_id,
            channel_id=args.channel_id,
            known_entities="[]",
            preprocessed_messages=msgs_json,
        )
        # Rough token estimate: ~4 chars per token
        fact_prompt_tokens = len(fact_prompt) // 4
        entity_prompt_tokens = len(entity_prompt) // 4

        print(f"  Batch {batch_num}/{len(batches)}")
        print(f"    Messages:             {len(batch)}")
        print(f"    Message tokens (est): {batch_tokens}")
        print(f"    Fact prompt tokens:   ~{fact_prompt_tokens}")
        print(f"    Entity prompt tokens: ~{entity_prompt_tokens}")
        print(f"    Total prompt tokens:  ~{fact_prompt_tokens + entity_prompt_tokens}")
        print()

    print(f"{'=' * 60}")
    print("  TOTALS")
    print(f"{'=' * 60}")
    print(f"  Message tokens (est):   {total_tokens}")
    print(f"  Batch API requests:     {len(batches) * 2}")
    print("  (Each request = 1 Gemini Batch API job submission)")
    print("\n  Run without --batch-api to execute actual extraction.\n")


async def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Dry-run extraction pipeline")
    parser.add_argument("channel_id", help="Slack channel ID")
    parser.add_argument(
        "--cached", action="store_true", help="Use cached messages (skip bridge fetch)"
    )
    parser.add_argument("--facts-only", action="store_true", help="Only run fact extraction")
    parser.add_argument("--entities-only", action="store_true", help="Only run entity extraction")
    parser.add_argument("--limit", type=int, default=0, help="Limit number of messages")
    parser.add_argument("--batch-size", type=int, default=10, help="Messages per batch")
    parser.add_argument(
        "--batch-api",
        action="store_true",
        help="Simulate Batch API path: show token estimates and request counts without running LLM calls",
    )
    args = parser.parse_args()

    from google.adk.runners import Runner
    from google.adk.sessions import InMemorySessionService
    from google.genai import types

    from beever_atlas.infra.config import get_settings
    from beever_atlas.llm.provider import init_llm_provider

    settings = get_settings()
    init_llm_provider(settings)

    # --- Batch API dry-run path ---
    if args.batch_api:
        await _run_batch_api_dry_run(args, settings)
        return

    # --- 1. Get messages ---
    cache_dir = Path(".omc/cache")
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"messages-{args.channel_id}.json"

    if args.cached and cache_file.exists():
        print(f"\n📦 Using cached messages from {cache_file}")
        raw_messages = json.loads(cache_file.read_text())
    else:
        print(f"\n🔄 Fetching messages from bridge for channel {args.channel_id}...")
        from beever_atlas.adapters.bridge import ChatBridgeAdapter

        adapter = ChatBridgeAdapter(bridge_url=settings.bridge_url, api_key=settings.bridge_api_key)
        messages = await adapter.fetch_history(args.channel_id, limit=500)
        print(f"   Fetched {len(messages)} top-level messages")

        # Fetch thread replies for messages with reply_count > 0
        thread_parents = [m for m in messages if getattr(m, "reply_count", 0) > 0]
        if thread_parents:
            print(f"   Fetching replies for {len(thread_parents)} threads...")
            total_replies = 0
            merged: list[Any] = []
            for m in messages:
                merged.append(m)
                if getattr(m, "reply_count", 0) > 0:
                    mid = getattr(m, "message_id", "")
                    try:
                        replies = await adapter.fetch_thread(args.channel_id, mid)
                        replies = [r for r in replies if getattr(r, "message_id", "") != mid]
                        merged.extend(replies)
                        total_replies += len(replies)
                    except Exception as e:
                        print(f"   ⚠️  Failed to fetch thread {mid}: {e}")
            messages = merged
            print(f"   Fetched {total_replies} thread replies")

        raw_messages = [vars(m) for m in messages]
        # Cache for next time
        cache_file.write_text(json.dumps(raw_messages, default=str, indent=2))
        print(f"   Cached {len(raw_messages)} messages to {cache_file}")

    if args.limit > 0:
        raw_messages = raw_messages[: args.limit]

    print(f"   Processing {len(raw_messages)} messages in batches of {args.batch_size}\n")

    # --- 2. Preprocess ---
    from beever_atlas.agents.ingestion.preprocessor import PreprocessorAgent

    preprocessor = PreprocessorAgent(name="preprocessor")
    session_service = InMemorySessionService()

    # Run preprocessor
    import uuid

    prep_session = await session_service.create_session(
        app_name="dry_run",
        user_id="dev",
        session_id=str(uuid.uuid4()),
        state={"messages": raw_messages, "channel_id": args.channel_id},
    )
    prep_runner = Runner(agent=preprocessor, app_name="dry_run", session_service=session_service)
    async for _ in prep_runner.run_async(
        user_id="dev",
        session_id=prep_session.id,
        new_message=types.Content(role="user", parts=[types.Part(text="preprocess")]),
    ):
        pass
    final_prep = await session_service.get_session(
        app_name="dry_run", user_id="dev", session_id=prep_session.id
    )
    preprocessed = final_prep.state.get("preprocessed_messages") or [] if final_prep else []
    print(f"✅ Preprocessor: {len(preprocessed)} messages retained\n")

    # --- 3. Run extraction in batches ---
    batches = [
        preprocessed[i : i + args.batch_size] for i in range(0, len(preprocessed), args.batch_size)
    ]

    all_facts: list[dict[str, Any]] = []
    all_entities: list[dict[str, Any]] = []
    all_relationships: list[dict[str, Any]] = []

    for batch_num, batch in enumerate(batches):
        batch_label = f"Batch {batch_num + 1}/{len(batches)}"
        print(f"{'=' * 60}")
        print(f"  {batch_label} ({len(batch)} messages)")
        print(f"{'=' * 60}")

        # Show message previews
        for msg in batch:
            text = (msg.get("text") or "")[:80]
            author = msg.get("author_name") or msg.get("username") or "?"
            print(f"  📝 [{author}] {text}{'...' if len(msg.get('text', '')) > 80 else ''}")
        print()

        if not args.entities_only:
            # --- Fact extraction ---
            from beever_atlas.agents.ingestion.fact_extractor import create_fact_extractor

            fact_agent = create_fact_extractor()
            fact_session = await session_service.create_session(
                app_name="dry_run",
                user_id="dev",
                session_id=str(uuid.uuid4()),
                state={
                    "preprocessed_messages": batch,
                    "channel_id": args.channel_id,
                    "channel_name": args.channel_id,
                    "max_facts_per_message": settings.max_facts_per_message,
                },
            )
            fact_runner = Runner(
                agent=fact_agent, app_name="dry_run", session_service=session_service
            )
            t0 = time.monotonic()
            async for _ in fact_runner.run_async(
                user_id="dev",
                session_id=fact_session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="extract facts")]),
            ):
                pass
            elapsed = time.monotonic() - t0
            final_fact = await session_service.get_session(
                app_name="dry_run", user_id="dev", session_id=fact_session.id
            )
            raw_facts = final_fact.state.get("extracted_facts") or {} if final_fact else {}
            facts = raw_facts.get("facts") if isinstance(raw_facts, dict) else raw_facts
            if not isinstance(facts, list):
                facts = []

            print(f"  🧠 Facts extracted: {len(facts)} ({elapsed:.1f}s)")
            for f in facts:
                score = f.get("quality_score", 0)
                imp = f.get("importance", "?")
                text = f.get("memory_text", "")
                emoji = "🟢" if score >= 0.7 else "🟡" if score >= 0.5 else "🔴"
                # Show media source badge if the fact came from a media-enriched message
                media_badge = ""
                src_msg_id = f.get("source_message_id") or f.get("message_ts", "")
                for pm in batch:
                    pm_id = pm.get("ts") or pm.get("message_id", "")
                    if pm_id == src_msg_id and pm.get("source_media_type"):
                        media_type = pm["source_media_type"]
                        media_badge = (
                            f" 🖼️[from {media_type}]"
                            if media_type == "image"
                            else f" 📄[from {media_type}]"
                        )
                        break
                print(f"    {emoji} [{score:.2f}|{imp}]{media_badge} {text}")
            all_facts.extend(facts)
            print()

        if not args.facts_only:
            # --- Entity extraction ---
            from beever_atlas.agents.ingestion.entity_extractor import create_entity_extractor

            entity_agent = create_entity_extractor()
            entity_session = await session_service.create_session(
                app_name="dry_run",
                user_id="dev",
                session_id=str(uuid.uuid4()),
                state={
                    "preprocessed_messages": batch,
                    "channel_id": args.channel_id,
                    "channel_name": args.channel_id,
                    "known_entities": [],
                },
            )
            entity_runner = Runner(
                agent=entity_agent, app_name="dry_run", session_service=session_service
            )
            t0 = time.monotonic()
            async for _ in entity_runner.run_async(
                user_id="dev",
                session_id=entity_session.id,
                new_message=types.Content(role="user", parts=[types.Part(text="extract entities")]),
            ):
                pass
            elapsed = time.monotonic() - t0
            final_entity = await session_service.get_session(
                app_name="dry_run", user_id="dev", session_id=entity_session.id
            )
            raw_entities = (
                final_entity.state.get("extracted_entities") or {} if final_entity else {}
            )
            entities = raw_entities.get("entities") if isinstance(raw_entities, dict) else []
            relationships = (
                raw_entities.get("relationships") if isinstance(raw_entities, dict) else []
            )
            if not isinstance(entities, list):
                entities = []
            if not isinstance(relationships, list):
                relationships = []

            print(
                f"  🔗 Entities: {len(entities)} | Relationships: {len(relationships)} ({elapsed:.1f}s)"
            )
            for e in entities:
                etype = e.get("type", "?")
                name = e.get("name", "?")
                aliases = e.get("aliases", [])
                alias_str = f" (aka {', '.join(aliases)})" if aliases else ""
                print(f"    📌 [{etype}] {name}{alias_str}")
            for r in relationships:
                src = r.get("source", "?")
                tgt = r.get("target", "?")
                rtype = r.get("type", "?")
                conf = r.get("confidence", 0)
                print(f"    ↔️  {src} --[{rtype}]--> {tgt} (conf={conf:.1f})")
            all_entities.extend(entities)
            all_relationships.extend(relationships)
            print()

    # --- 4. Summary ---
    print(f"\n{'=' * 60}")
    print("  SUMMARY")
    print(f"{'=' * 60}")
    print(f"  Messages processed: {len(preprocessed)}")
    print(f"  Facts extracted:    {len(all_facts)}")
    print(f"  Entities found:     {len(all_entities)}")
    print(f"  Relationships:      {len(all_relationships)}")
    if all_facts:
        avg_score = sum(f.get("quality_score", 0) for f in all_facts) / len(all_facts)
        print(f"  Avg quality score:  {avg_score:.2f}")
    media_msgs = sum(1 for m in preprocessed if m.get("modality") == "mixed")
    if media_msgs:
        print(f"  Media-enriched msgs: {media_msgs}")
    print()

    # Save results for inspection
    results_file = cache_dir / f"dry-run-{args.channel_id}.json"
    results_file.write_text(
        json.dumps(
            {
                "facts": all_facts,
                "entities": all_entities,
                "relationships": all_relationships,
            },
            default=str,
            indent=2,
        )
    )
    print(f"  📄 Full results saved to {results_file}")
    print("  💡 Re-run with --cached to skip bridge fetch\n")


if __name__ == "__main__":
    asyncio.run(main())
