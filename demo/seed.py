"""Demo seed script for Beever Atlas.

Seeds Weaviate, Neo4j, and MongoDB with pre-computed fixtures from the Wikipedia
demo corpus (Ada Lovelace + Python history), enabling zero-API-key local demos.

Usage:
    # Default — pre-computed fixtures (zero API keys, <30s):
    python demo/seed.py

    # Live ingestion — runs the full ADK pipeline (requires GOOGLE_API_KEY + JINA_API_KEY):
    python demo/seed.py --live

    # Live ingestion + write fixtures for committing:
    python demo/seed.py --live --write-fixtures

    # Force re-seed even if demo data already exists:
    python demo/seed.py --force
    python demo/seed.py --live --force

Environment:
    All variables read from .env in the project root (loaded via python-dotenv).
    Required for --live: GOOGLE_API_KEY, JINA_API_KEY.
    Required for /api/ask: GOOGLE_API_KEY (not needed for seeding).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env from project root before any beever_atlas imports.
from dotenv import load_dotenv

_project_root = Path(__file__).resolve().parent.parent
load_dotenv(_project_root / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("demo.seed")

DEMO_CHANNEL_ID = "demo-wikipedia"
DEMO_CHANNEL_NAME = "#demo"
FIXTURES_DIR = Path(__file__).parent / "fixtures"
CORPUS_DIR = Path(__file__).parent / "corpus"


# ---------------------------------------------------------------------------
# Idempotency check
# ---------------------------------------------------------------------------


async def _already_seeded(stores) -> bool:
    """Return True if demo data already exists in MongoDB."""
    try:
        state = await stores.mongodb.get_channel_sync_state(DEMO_CHANNEL_ID)
        return state is not None
    except Exception:
        return False


# ---------------------------------------------------------------------------
# --precomputed branch (default)
# ---------------------------------------------------------------------------


async def seed_precomputed(stores, *, force: bool = False) -> None:
    """Load pre-computed fixtures into Weaviate, Neo4j, and MongoDB.

    This path requires zero API keys and completes in <30 seconds.
    """
    if not force and await _already_seeded(stores):
        logger.info("Demo data already seeded, skipping. Use --force to re-seed.")
        return

    # --- 1. Read and validate manifest ---
    manifest_path = FIXTURES_DIR / "manifest.json"
    if not manifest_path.exists():
        logger.error(
            "demo/fixtures/manifest.json not found.\n"
            "Fixtures are stub placeholders — run 'make demo-regenerate-fixtures' "
            "once to populate real fixtures before running make demo."
        )
        sys.exit(1)

    manifest = json.loads(manifest_path.read_text())

    # Check JINA_DIMENSIONS consistency
    import os
    jina_dimensions = int(os.environ.get("JINA_DIMENSIONS", "2048"))
    manifest_dims = manifest.get("jina_dimensions", 2048)
    if jina_dimensions != manifest_dims:
        logger.error(
            "JINA_DIMENSIONS mismatch: env=%d, manifest=%d. "
            "Re-generate fixtures with 'make demo-regenerate-fixtures' "
            "or set JINA_DIMENSIONS=%d in .env.",
            jina_dimensions,
            manifest_dims,
            manifest_dims,
        )
        sys.exit(1)

    logger.info(
        "Loading fixtures (generated %s, model=%s, dims=%d)...",
        manifest.get("generated_at", "unknown"),
        manifest.get("embedding_model", "unknown"),
        manifest_dims,
    )

    # --- 2. MongoDB: upsert channel, sync-state, messages ---
    mongo_path = FIXTURES_DIR / "mongo_seed.json"
    if not mongo_path.exists():
        logger.error(
            "demo/fixtures/mongo_seed.json not found — run 'make demo-regenerate-fixtures'."
        )
        sys.exit(1)

    mongo_data = json.loads(mongo_path.read_text())

    # Check for stub placeholder
    if mongo_data.get("_stub"):
        logger.error(
            "demo/fixtures/mongo_seed.json is a stub placeholder.\n"
            "Run 'make demo-regenerate-fixtures' to populate real fixtures."
        )
        sys.exit(1)

    logger.info("Seeding MongoDB...")
    db = stores.mongodb.db

    # Upsert channel document
    for doc in mongo_data.get("channels", []):
        await db["channels"].update_one(
            {"channel_id": doc["channel_id"]},
            {"$set": doc},
            upsert=True,
        )

    # Upsert sync-state document
    for doc in mongo_data.get("channel_sync_state", []):
        await db["channel_sync_state"].update_one(
            {"channel_id": doc["channel_id"]},
            {"$set": doc},
            upsert=True,
        )

    # Upsert message documents
    messages = mongo_data.get("messages", [])
    for doc in messages:
        await db["messages"].update_one(
            {"message_id": doc["message_id"]},
            {"$set": doc},
            upsert=True,
        )
    logger.info("MongoDB: %d messages seeded.", len(messages))

    # --- 3. Weaviate: batch-import pre-computed embeddings ---
    weaviate_path = FIXTURES_DIR / "weaviate_facts.jsonl"
    if not weaviate_path.exists():
        logger.error(
            "demo/fixtures/weaviate_facts.jsonl not found — run 'make demo-regenerate-fixtures'."
        )
        sys.exit(1)

    logger.info("Seeding Weaviate...")
    weaviate_lines = [
        line for line in weaviate_path.read_text().splitlines() if line.strip() and not line.startswith("//")
    ]

    if not weaviate_lines:
        logger.error(
            "demo/fixtures/weaviate_facts.jsonl is empty (stub placeholder).\n"
            "Run 'make demo-regenerate-fixtures' to populate real fixtures."
        )
        sys.exit(1)

    facts = [json.loads(line) for line in weaviate_lines]

    # Use WeaviateStore batch import with pre-computed vectors
    client = stores.weaviate._client
    with client.batch.fixed_size(batch_size=50) as batch:
        for fact in facts:
            vector = fact.get("embedding")
            properties = {
                k: v for k, v in fact.items()
                if k not in ("embedding",)
            }
            batch.add_object(
                collection="Fact",
                properties=properties,
                vector=vector,
            )
    logger.info("Weaviate: %d facts seeded.", len(facts))

    # --- 4. Neo4j: execute parameterised Cypher statements ---
    neo4j_path = FIXTURES_DIR / "neo4j_graph.cypher"
    if not neo4j_path.exists():
        logger.error(
            "demo/fixtures/neo4j_graph.cypher not found — run 'make demo-regenerate-fixtures'."
        )
        sys.exit(1)

    logger.info("Seeding Neo4j...")
    cypher_content = neo4j_path.read_text()

    # Parse statement blocks: each block is an optional `// params: {...}` line
    # followed by one or more Cypher statement lines ending in `;`
    blocks = _parse_cypher_blocks(cypher_content)
    if not blocks:
        logger.error(
            "demo/fixtures/neo4j_graph.cypher has no statements (stub placeholder).\n"
            "Run 'make demo-regenerate-fixtures' to populate real fixtures."
        )
        sys.exit(1)

    neo4j_driver = stores.graph._driver
    async with neo4j_driver.session() as session:
        for stmt, params in blocks:
            await session.run(stmt, **params)
    logger.info("Neo4j: %d statements executed.", len(blocks))

    logger.info("Seeding complete.")
    print()
    print("=" * 60)
    print("Demo data seeded successfully.")
    print()
    print("To ask questions via /api/ask, set GOOGLE_API_KEY in .env")
    print("(free tier at https://aistudio.google.com — no credit card required).")
    print()
    print("Example:")
    print('  curl -N -X POST http://localhost:8000/api/channels/demo-wikipedia/ask \\')
    print('    -H "Authorization: Bearer dev-key-change-me" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d \'{"question":"Who was Ada Lovelace?"}\'')
    print("=" * 60)


def _parse_cypher_blocks(content: str) -> list[tuple[str, dict]]:
    """Parse Cypher fixture file into (statement, params) tuples.

    Expected format per block:
        // params: {"key": "value"}
        MERGE (n:Label {prop: $key}) ...;

    Lines starting with `//` that are NOT `// params:` are treated as comments
    and skipped. Blank lines between blocks are ignored.
    """
    blocks: list[tuple[str, dict]] = []
    current_params: dict = {}
    current_stmt_lines: list[str] = []

    def _flush():
        stmt = "\n".join(current_stmt_lines).strip()
        if stmt:
            blocks.append((stmt, current_params.copy()))
        current_stmt_lines.clear()
        current_params.clear()

    for line in content.splitlines():
        stripped = line.strip()
        if not stripped:
            if current_stmt_lines:
                _flush()
            continue
        if stripped.startswith("// params:"):
            if current_stmt_lines:
                _flush()
            params_json = stripped[len("// params:"):].strip()
            try:
                current_params.update(json.loads(params_json))
            except json.JSONDecodeError as exc:
                logger.warning("Skipping malformed params line: %s (%s)", stripped, exc)
        elif stripped.startswith("//"):
            # Regular comment — skip
            continue
        else:
            current_stmt_lines.append(line)
            if stripped.endswith(";"):
                _flush()

    if current_stmt_lines:
        _flush()

    return blocks


# ---------------------------------------------------------------------------
# --live branch (opt-in)
# ---------------------------------------------------------------------------


async def seed_live(stores, *, force: bool = False, write_fixtures: bool = False) -> None:
    """Run the full ADK ingestion pipeline against demo/corpus/*.md.

    Requires GOOGLE_API_KEY (Gemini) and JINA_API_KEY.
    Follows the same pattern as src/beever_atlas/scripts/ingest_from_csv.py:98-130.
    """
    if not force and await _already_seeded(stores):
        logger.info("Demo data already seeded, skipping. Use --force to re-seed.")
        return

    import os
    if not os.environ.get("GOOGLE_API_KEY"):
        logger.error("GOOGLE_API_KEY is not set. The --live path requires a Gemini API key.")
        logger.error("Get a free key at https://aistudio.google.com")
        sys.exit(1)
    if not os.environ.get("JINA_API_KEY"):
        logger.error("JINA_API_KEY is not set. The --live path requires a Jina API key.")
        logger.error("Get a free key at https://jina.ai")
        sys.exit(1)

    from beever_atlas.adapters.base import NormalizedMessage
    from beever_atlas.services.batch_processor import BatchProcessor
    from beever_atlas.services.policy_resolver import resolve_effective_policy
    from beever_atlas.models.sync_policy import IngestionConfig

    # --- 1. Read corpus files and build NormalizedMessage list ---
    corpus_files = sorted(CORPUS_DIR.glob("*.md"))
    if not corpus_files:
        logger.error("No .md files found in demo/corpus/. Cannot run live seed.")
        sys.exit(1)

    logger.info("Reading %d corpus files from demo/corpus/...", len(corpus_files))
    messages: list[NormalizedMessage] = []
    base_ts = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    for idx, filepath in enumerate(corpus_files, start=1):
        content = filepath.read_text(encoding="utf-8")
        # Strip HTML comments (attribution headers)
        lines = [l for l in content.splitlines() if not l.startswith("<!--") and not l.startswith("-->")]
        clean_content = "\n".join(lines).strip()

        from datetime import timedelta
        ts = base_ts + timedelta(seconds=idx)
        msg_id = f"demo-msg-{idx:04d}"

        messages.append(NormalizedMessage(
            content=clean_content,
            author="wikipedia",
            author_name="Wikipedia",
            platform="file",
            channel_id=DEMO_CHANNEL_ID,
            channel_name=DEMO_CHANNEL_NAME,
            message_id=msg_id,
            timestamp=ts,
        ))

    logger.info("Built %d NormalizedMessage objects.", len(messages))

    # --- 2. Create mock PlatformConnection in MongoDB ---
    # Demo uses platform="file" + source="env" (env-sourced, no real credentials).
    # Encrypted-credential fields are required by PlatformConnection; we write
    # valid AES-256-GCM-shaped placeholders for an empty dict so the schema
    # validator accepts the doc. The bytes are never decrypted for the file
    # platform since ingestion pulls from demo/corpus/*.md, not a live API.
    from beever_atlas.infra.crypto import encrypt_credentials
    from beever_atlas.models.platform_connection import PlatformConnection

    db = stores.mongodb.db
    existing_conn = await db["platform_connections"].find_one(
        {"platform": "file", "display_name": "Demo Wikipedia Corpus"}
    )
    if not existing_conn:
        enc, iv, tag = encrypt_credentials({})
        conn = PlatformConnection(
            platform="file",
            source="env",
            display_name="Demo Wikipedia Corpus",
            encrypted_credentials=enc,
            credential_iv=iv,
            credential_tag=tag,
            selected_channels=[DEMO_CHANNEL_ID],
            status="connected",
            owner_principal_id="legacy:shared",
        )
        await db["platform_connections"].insert_one(conn.model_dump(mode="python"))
        logger.info("Created demo platform connection (file/env).")

    # --- 3. Create MongoDB sync job ---
    job = await stores.mongodb.create_sync_job(
        channel_id=DEMO_CHANNEL_ID,
        sync_type="full",
        total_messages=len(messages),
        parent_messages=len(messages),
        batch_size=10,
    )
    logger.info("Created sync job: %s", job.id)

    # --- 4. Run batch processor (same as SyncRunner._run_sync) ---
    effective_policy = await resolve_effective_policy(DEMO_CHANNEL_ID)
    batch_processor = BatchProcessor()

    logger.info("Running ingestion pipeline (%d messages)...", len(messages))
    result = await batch_processor.process_messages(
        messages=messages,
        channel_id=DEMO_CHANNEL_ID,
        channel_name=DEMO_CHANNEL_NAME,
        sync_job_id=job.id,
        ingestion_config=effective_policy.ingestion,
    )
    logger.info("Ingestion complete: %s", result)

    # --- 5. Optionally write fixtures ---
    if write_fixtures:
        logger.info("--write-fixtures requested. Exporting to demo/fixtures/...")
        await _export_fixtures(stores)

    logger.info("Live seed complete.")
    print()
    print("=" * 60)
    print("Live ingestion complete.")
    print()
    print("To ask questions, use:")
    print('  curl -N -X POST http://localhost:8000/api/channels/demo-wikipedia/ask \\')
    print('    -H "Authorization: Bearer dev-key-change-me" \\')
    print('    -H "Content-Type: application/json" \\')
    print('    -d \'{"question":"Who was Ada Lovelace?"}\'')
    print("=" * 60)


async def _export_fixtures(stores) -> None:
    """Export current store state to demo/fixtures/ for committing.

    Called when seed.py --live --write-fixtures is invoked (i.e. make demo-regenerate-fixtures).
    """
    import os
    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

    # --- manifest.json ---
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "embedding_model": os.environ.get("JINA_MODEL", "jina-embeddings-v3"),
        "llm_model": os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
        "jina_dimensions": int(os.environ.get("JINA_DIMENSIONS", "2048")),
        "corpus_files": sorted(p.name for p in CORPUS_DIR.glob("*.md")),
        "note": (
            "Re-generate with: make demo-regenerate-fixtures. "
            "Fixtures must be regenerated if JINA_DIMENSIONS or corpus files change."
        ),
    }
    (FIXTURES_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
    logger.info("Wrote manifest.json")

    # --- mongo_seed.json ---
    db = stores.mongodb.db

    def _serialize(doc):
        """Convert MongoDB document to JSON-serialisable dict."""
        from bson import ObjectId
        result = {}
        for k, v in doc.items():
            if k == "_id":
                continue
            if isinstance(v, ObjectId):
                result[k] = str(v)
            elif isinstance(v, datetime):
                result[k] = v.isoformat()
            elif isinstance(v, dict):
                result[k] = _serialize(v)
            elif isinstance(v, list):
                result[k] = [_serialize(i) if isinstance(i, dict) else i for i in v]
            else:
                result[k] = v
        return result

    channels = [_serialize(d) async for d in db["channels"].find({"channel_id": DEMO_CHANNEL_ID})]
    sync_states = [_serialize(d) async for d in db["channel_sync_state"].find({"channel_id": DEMO_CHANNEL_ID})]
    messages = [_serialize(d) async for d in db["messages"].find({"channel_id": DEMO_CHANNEL_ID})]

    mongo_data = {
        "channels": channels,
        "channel_sync_state": sync_states,
        "messages": messages,
    }
    (FIXTURES_DIR / "mongo_seed.json").write_text(json.dumps(mongo_data, indent=2))
    logger.info("Wrote mongo_seed.json (%d messages)", len(messages))

    # --- weaviate_facts.jsonl ---
    # Export facts with their stored vectors from Weaviate
    weaviate_lines = []
    try:
        client = stores.weaviate._client
        result = (
            client.collections.get("Fact")
            .query.fetch_objects(
                filters=client.collections.get("Fact").query.filter.by_property("channel_id").equal(DEMO_CHANNEL_ID),
                include_vector=True,
                limit=10000,
            )
        )
        for obj in result.objects:
            record = dict(obj.properties)
            if obj.vector:
                vec = obj.vector
                record["embedding"] = list(vec) if not isinstance(vec, list) else vec
            weaviate_lines.append(json.dumps(record))
    except Exception as exc:
        logger.warning("Could not export Weaviate facts: %s", exc)

    (FIXTURES_DIR / "weaviate_facts.jsonl").write_text("\n".join(weaviate_lines) + "\n" if weaviate_lines else "")
    logger.info("Wrote weaviate_facts.jsonl (%d facts)", len(weaviate_lines))

    # --- neo4j_graph.cypher ---
    cypher_lines: list[str] = []
    try:
        neo4j_driver = stores.graph._driver
        async with neo4j_driver.session() as session:
            # Export nodes
            node_result = await session.run(
                "MATCH (n) WHERE n.channel_id = $channel_id RETURN n",
                channel_id=DEMO_CHANNEL_ID,
            )
            async for record in node_result:
                node = record["n"]
                labels = ":".join(node.labels)
                props = dict(node)
                params_line = f"// params: {json.dumps(props)}"
                prop_assigns = ", ".join(f"n.{k} = ${k}" for k in props if k != "name")
                stmt = f"MERGE (n:{labels} {{name: $name}}) SET {prop_assigns};" if prop_assigns else f"MERGE (n:{labels} {{name: $name}});"
                cypher_lines.append(params_line)
                cypher_lines.append(stmt)
                cypher_lines.append("")

            # Export relationships
            rel_result = await session.run(
                "MATCH (a)-[r]->(b) WHERE a.channel_id = $channel_id RETURN a.name AS src, type(r) AS rel, b.name AS tgt, properties(r) AS props",
                channel_id=DEMO_CHANNEL_ID,
            )
            async for record in rel_result:
                params = {
                    "src": record["src"],
                    "tgt": record["tgt"],
                    **record["props"],
                }
                params_line = f"// params: {json.dumps(params)}"
                stmt = f"MATCH (a {{name: $src}}), (b {{name: $tgt}}) MERGE (a)-[:{record['rel']}]->(b);"
                cypher_lines.append(params_line)
                cypher_lines.append(stmt)
                cypher_lines.append("")
    except Exception as exc:
        logger.warning("Could not export Neo4j graph: %s", exc)

    (FIXTURES_DIR / "neo4j_graph.cypher").write_text("\n".join(cypher_lines))
    logger.info("Wrote neo4j_graph.cypher (%d statement blocks)", len([l for l in cypher_lines if l.startswith("MERGE") or l.startswith("MATCH")]))

    logger.info("Fixture export complete. Commit demo/fixtures/ to make changes available.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the Beever Atlas demo with Wikipedia corpus data."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="Run live ADK ingestion pipeline (requires GOOGLE_API_KEY + JINA_API_KEY).",
    )
    parser.add_argument(
        "--precomputed",
        action="store_true",
        help="Load pre-computed fixtures (default behaviour; zero API keys).",
    )
    parser.add_argument(
        "--write-fixtures",
        action="store_true",
        help="After --live ingestion, export results to demo/fixtures/ (for maintainers).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-seed even if demo data already exists.",
    )
    args = parser.parse_args()

    from beever_atlas.infra.config import get_settings
    from beever_atlas.llm.provider import init_llm_provider
    from beever_atlas.stores import StoreClients, init_stores

    settings = get_settings()
    stores = StoreClients.from_settings(settings)
    await stores.startup()
    init_stores(stores)

    if args.live:
        init_llm_provider(settings)
        await seed_live(stores, force=args.force, write_fixtures=args.write_fixtures)
    else:
        await seed_precomputed(stores, force=args.force)

    await stores.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
