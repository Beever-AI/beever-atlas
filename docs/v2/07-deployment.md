# Deployment & Module Structure

## Docker Compose

```yaml
# docker-compose.yml (v2)
services:
  beever-atlas:          # Python/FastAPI (MCP + REST)
    build: .
    ports: ["8000:8000"]
    depends_on: [weaviate, neo4j, mongodb]

  web:                   # React frontend
    build: ./web
    ports: ["3000:80"]

  weaviate:              # Semantic memory
    image: cr.weaviate.io/semitechnologies/weaviate:1.28.0
    ports: ["8080:8080", "50051:50051"]
    volumes: [weaviate_data:/var/lib/weaviate]

  neo4j:                 # Graph memory
    image: neo4j:5.26-community
    ports: ["7474:7474", "7687:7687"]
    environment:
      NEO4J_AUTH: neo4j/beever_atlas_dev
      NEO4J_PLUGINS: '["apoc"]'
    volumes: [neo4j_data:/data]

  mongodb:               # State + cache
    image: mongo:7.0
    ports: ["27017:27017"]
    volumes: [mongo_data:/data/db]

  redis:                   # Chat SDK session state
    image: redis:7-alpine
    ports: ["6379:6379"]

  bot:                     # Chat SDK bot (TypeScript)
    build: ./bot
    depends_on: [redis, beever-atlas]
    environment:
      BEEVER_API_URL: http://beever-atlas:8000
      REDIS_URL: redis://redis:6379

volumes:
  weaviate_data:
  neo4j_data:
  mongo_data:
```

---

## MCP Tool Specification

**Design decision:** Graph queries are abstracted behind `ask_questions`. The smart router decides when to use Neo4j — users don't need to know about the dual-memory architecture.

### 7 Tools

```python
@tool("ask_questions")
async def ask_questions(
    question: str,           # Natural language query
    channel_id: str = None,  # Target channel (None = cross-channel search, ACL-filtered)
    include_citations: bool = True,
    max_results: int = 10,
) -> AskResponse:
    """Ask a question about channel knowledge. Routes automatically
    to semantic search, graph traversal, or both based on query type.
    Cost: $0.001-$0.006 depending on route."""

@tool("search_memories")
async def search_memories(
    query: str,              # Search query
    channel_id: str,
    tier: str = "all",       # "all" | "summary" | "topic" | "atomic"
    limit: int = 15,
    include_images: bool = False,
) -> SearchResponse:
    """Direct hybrid search — bypasses router for power users.
    Cost: ~$0.001"""

@tool("get_wiki")
async def get_wiki(
    channel_id: str,
    section: str = "all",    # "all"|"overview"|"topics"|"people"|"decisions"|"recent"
) -> WikiResponse:
    """Read cached wiki content. FREE for cached sections.
    Returns stale data if wiki is dirty — use refresh_wiki to force update."""

@tool("get_topics")
async def get_topics(
    channel_id: str,
) -> TopicsResponse:
    """List topic clusters for a channel. FREE (cached Tier 1)."""

@tool("sync_channel")
async def sync_channel(
    channel_id: str,
    max_messages: int = 5000,  # Safety limit to prevent cost explosion
    since: str = None,         # ISO timestamp, defaults to last sync point
) -> SyncResponse:
    """Trigger ingestion for a channel. Runs in background.
    Cost: ~$0.0025/message (text), ~$0.008/message (with media)."""

@tool("get_sync_status")
async def get_sync_status(
    channel_id: str = None,    # None = all channels
) -> SyncStatusResponse:
    """Check sync progress and health status. FREE."""

@tool("refresh_wiki")
async def refresh_wiki(
    channel_id: str,
) -> RefreshResponse:
    """Force wiki regeneration. Triggers full reconsolidation.
    Cost: ~$0.01 for LLM synthesis."""
```

---

## MCP Resources

Read-only, URI-based access to wiki content:

```python
@resource("wiki://{channel_id}")           # Full wiki markdown
@resource("wiki://{channel_id}/overview")  # Tier 0 summary only
@resource("wiki://{channel_id}/topics")    # Tier 1 cluster list
```

---

## Response Schemas

```python
class AskResponse:
    answer: str                    # Grounded response with inline citations
    citations: list[Citation]      # Source facts with platform permalinks
    route_used: str                # "semantic" | "graph" | "both"
    confidence: float              # 0.0-1.0
    degraded: bool                 # True if a component was unavailable
    cost_usd: float                # Estimated cost of this query

class Citation:
    text: str                      # Original fact text
    channel: str                   # Source channel name
    user: str                      # Who said it
    timestamp: str                 # When it was said
    permalink: str                 # Platform message URL
    tier: str                      # "atomic" | "topic" | "summary"

class SyncResponse:
    status: str                    # "started" | "already_running" | "queued"
    channel_id: str
    estimated_messages: int        # Approximate message count to process
    job_id: str                    # For tracking via get_sync_status

class WikiResponse:
    content: str                   # Markdown wiki content
    generated_at: str              # When this version was generated
    is_stale: bool                 # True if wiki_dirty flag is set
    channel_id: str
```

---

## Module Structure

```
src/beever_atlas/
├── agents/                      # ADK agent definitions (see 13-adk-integration.md)
│   ├── ingestion/               # 6-stage ingestion pipeline
│   │   ├── pipeline.py          # create_ingestion_pipeline() SequentialAgent factory
│   │   ├── preprocessor.py      # Stage 1 — mrkdwn, threads, media
│   │   ├── fact_extractor.py    # Stage 2 (parallel) — fact extraction
│   │   ├── entity_extractor.py  # Stage 2 (parallel) — entity + relation extraction
│   │   ├── embedder.py          # Stage 3 (parallel) — Jina v4 embeddings
│   │   ├── cross_batch_validator.py  # Stage 3 (parallel) — alias resolution
│   │   ├── persister.py         # Stage 4 — outbox write to Weaviate + Neo4j + MongoDB
│   │   ├── contradiction_detector.py
│   │   └── coreference_resolver.py
│   ├── consolidation/           # Consolidation agents
│   │   └── summarizer.py        # LlmAgent for cluster/channel summaries
│   ├── media/                   # Media processing agents
│   │   └── document_digester.py # PDF + image processing
│   ├── query/                   # Q&A routing agents (in development)
│   │   └── echo.py
│   ├── tools.py                 # ADK FunctionTool wrappers for store operations
│   └── runner.py                # ADK Runner initialization
│
├── adapters/                    # Multi-platform ingestion adapters
│   ├── base.py                  # NormalizedMessage, BaseAdapter
│   ├── slack_adapter.py         # slack-sdk
│   ├── teams_adapter.py         # Microsoft Graph API
│   └── discord_adapter.py       # discord.py
│
├── api/                         # FastAPI route handlers (REST API)
│   ├── ask.py                   # Streaming Q&A (SSE)
│   ├── channels.py              # Channel listing + history
│   ├── connections.py           # Platform connection CRUD
│   ├── graph.py                 # Entity + relationship endpoints
│   ├── memories.py              # Fact search + listing
│   ├── search.py                # Cross-channel search
│   ├── stats.py                 # Aggregate stats
│   ├── sync.py                  # Sync trigger + status
│   ├── topics.py                # Topic cluster endpoints
│   └── wiki.py                  # Wiki retrieval + refresh
│
├── services/                    # Core business logic
│   ├── batch_pipeline.py        # Gemini Batch API orchestrator
│   ├── batch_processor.py       # Per-batch message processor
│   ├── consolidation.py         # Topic clustering + channel summaries
│   ├── media_processor.py       # Image/PDF/video processing
│   ├── reconciler.py            # Retry incomplete cross-store writes
│   ├── scheduler.py             # Background sync scheduling
│   └── sync_runner.py           # Sync job coordinator
│
├── stores/                      # Data store clients
│   ├── weaviate_store.py        # Semantic memory (3-tier)
│   ├── neo4j_store.py           # Graph memory (flexible)
│   ├── nebula_store.py          # Graph memory (NebulaGraph alternative)
│   ├── mongodb_store.py         # State + wiki cache
│   ├── entity_registry.py       # Canonical names + alias resolution
│   ├── graph_protocol.py        # Shared graph store protocol
│   └── null_graph.py            # No-op graph store (mock/dev mode)
│
├── retrieval/                   # Query retrieval layer (in development)
│   └── __init__.py              # Planned for Q&A agent phase
│
├── wiki/                        # Wiki generation
│   ├── builder.py               # Orchestrates full wiki build (WikiBuilder)
│   ├── compiler.py              # LLM page generation (WikiCompiler)
│   └── cache.py                 # MongoDB wiki cache (WikiCache)
│
├── server/                      # Server entry point
│   └── app.py                   # FastAPI app, lifespan, CORS, router registration
│
├── llm/                         # LLM provider abstraction
│   └── provider.py              # LLMProvider — resolves models from env vars
│
├── models/                      # Pydantic domain models
│
└── infra/                       # Cross-cutting infrastructure
    ├── health_registry.py        # Circuit breakers per dependency
    └── telemetry.py              # OpenTelemetry traces + metrics
```
