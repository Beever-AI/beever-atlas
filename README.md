<div align="center">

# 🦫 Beever Atlas

**Wiki-first knowledge intelligence for team channels**

Turn Slack, Discord, and Teams conversations into a living, searchable knowledge base — automatically.

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com)
[![Google ADK](https://img.shields.io/badge/Google%20ADK-agent%20framework-orange.svg)](https://google.github.io/adk-docs/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

</div>

---

## What Is Beever Atlas?

Beever Atlas continuously ingests your team's conversations and builds an automatically maintained wiki — no manual curation required. It stores knowledge in two complementary memory systems (semantic + graph) and lets you query it in natural language.

**Implemented features:**
- 🔄 **Multi-platform ingestion** — Slack, Discord, Microsoft Teams
- 🧠 **Dual-memory architecture** — Weaviate (semantic) + Neo4j (graph)
- 📚 **Auto-generated wiki** — hierarchical, paginated, with Mermaid diagrams and citations
- 🗂️ **Multimodal understanding** — images (Gemini vision), PDFs, videos, links
- 🗃️ **Topic clustering** — automatic organization into browsable topic pages
- 🕸️ **Entity graph** — people, decisions, projects, technologies and their relationships
- 📡 **Streaming Q&A** — ask questions, get cited answers via SSE
- 🌐 **React dashboard** — web UI for wiki, graph exploration, and admin

**In development:**
- 🤖 QA agent (single-channel and multi-channel MCP)
- ☁️ One-line cloud installation
- 🔍 Cross-channel search (Phase 2)

---

## Architecture

Beever Atlas is composed of three services that communicate via HTTP:

```
┌─────────────────────────────────────────────────────────────────┐
│                      BEEVER ATLAS v2                            │
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────────┐ │
│  │  Web Frontend│    │  Bot Service │    │  Python Backend  │ │
│  │  React + Vite│    │  TypeScript  │    │  FastAPI + ADK   │ │
│  │  Port 3000   │    │  Port 3001   │    │  Port 8000       │ │
│  └──────┬───────┘    └──────┬───────┘    └────────┬─────────┘ │
│         │                  │                      │           │
│         └──────────────────┴──────────────────────┘           │
│                          REST API                              │
│                                                                 │
│  ┌──────────┐  ┌──────────┐  ┌───────────┐  ┌─────────────┐  │
│  │ Weaviate │  │  Neo4j   │  │  MongoDB  │  │    Redis    │  │
│  │ Semantic │  │  Graph   │  │ State+    │  │  Sessions   │  │
│  │ Memory   │  │  Memory  │  │ Wiki cache│  │             │  │
│  └──────────┘  └──────────┘  └───────────┘  └─────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Dual-Memory Design

| | Semantic Memory (Weaviate) | Graph Memory (Neo4j) |
|---|---|---|
| **Stores** | Atomic facts, topic clusters, channel summaries, multimodal content | Entities (Person, Decision, Project, Technology), relationships, temporal evolution |
| **Query method** | Hybrid BM25 + vector search (Jina v4, 2048-dim) | Cypher graph traversal |
| **Answers** | *"What was discussed about JWT?"*, *"Show the authentication topic"* | *"Who decided on RS256?"*, *"What projects is Alice working on?"* |
| **Query share** | ~80% of queries | ~20% of queries |
| **Latency** | < 200ms | 200ms – 1s |

The two stores are bidirectionally linked: every Weaviate fact stores `graph_entity_ids`, and every Neo4j entity stores a `MENTIONED_IN` edge to its source Weaviate fact. This enables hybrid query paths.

### Ingestion Pipeline (6-Stage ADK SequentialAgent)

```
NormalizedMessage
      │
      ▼
 [1] Preprocessor ── Slack mrkdwn → markdown, thread assembly,
      │               media attachments (images/PDFs via vision),
      │               bot message filtering
      ▼
 [2] Fact Extractor ── LLM (Gemini Flash Lite) → atomic facts
      │                Quality gate: score ≥ 0.5, max 2 facts/msg
      ▼
 [3] Entity Extractor ── LLM → entities + relationships
      │                  Entity quality gate: confidence ≥ 0.6
      │                  Alias deduplication, temporal validity
      ▼
 [4] Embedder ── Jina v4 (2048-dim, named vectors)
      │
      ▼
 [5] Cross-Batch Validator ── Resolve entity aliases across batches,
      │                       validate relationship consistency
      ▼
 [6] Persister ── Write to Weaviate + Neo4j + MongoDB
                  Outbox pattern for atomic cross-store writes
```

For large channels, the pipeline can run via **Gemini Batch API** (`use_batch_api=true`), which submits extraction jobs asynchronously and polls for results — ideal for initial syncs of thousands of messages.

### Wiki Generation Flow

After ingestion, Atlas builds a structured wiki per channel:

1. **Consolidation** — Atomic facts are clustered into topic groups using cosine similarity on Jina embeddings (no LLM cost). Cluster summaries are generated by an LLM.
2. **Wiki Builder** — Queries Weaviate + Neo4j to gather all content, then uses the `WikiCompiler` to generate 10+ page types: Overview, Topics, Sub-topics, People, Decisions, Tech Stack, Projects, Recent Activity, FAQ, Glossary, Resources.
3. **Cache** — Full wiki is stored in MongoDB and served without LLM cost. A `wiki_dirty` flag triggers regeneration when new data arrives.

---

## Tech Stack

| Layer | Technology |
|---|---|
| **Agent Framework** | [Google ADK](https://google.github.io/adk-docs/) (Python) — all LLM-powered operations |
| **Backend API** | FastAPI 0.115 (Python 3.12+) |
| **Bot Service** | TypeScript + Node.js |
| **Frontend** | React 19 + Vite + TypeScript + TailwindCSS + shadcn/ui |
| **Semantic Store** | Weaviate 1.28 |
| **Graph Store** | Neo4j 5.26 + APOC |
| **State / Cache** | MongoDB 7.0 |
| **Sessions** | Redis 7 |
| **Embeddings** | Jina v4 (2048-dim, multimodal) |
| **LLM (fast)** | Gemini 2.5 Flash (extraction, classification) |
| **LLM (quality)** | Gemini 2.5 Flash (wiki synthesis, validation) |
| **Vision** | Gemini vision (image descriptions) |
| **Graph Viz** | Cytoscape.js |

---

## Quick Start

### Prerequisites

- Docker & Docker Compose
- A Google API key (Gemini) — [get one free](https://aistudio.google.com/apikey)
- A Jina API key (embeddings) — [get one free](https://jina.ai)
- (Optional) Slack bot token for real data; mock mode works without one

### 1. Clone and Configure

```bash
git clone https://github.com/your-org/beever-atlas.git
cd beever-atlas
cp .env.example .env
```

Edit `.env` with your keys (minimum required for Docker):

```env
# Required
GOOGLE_API_KEY=your_gemini_key
JINA_API_KEY=your_jina_key

# Optional — enables real Slack data. Remove for mock mode.
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...

# Recommended for production
CREDENTIAL_MASTER_KEY=   # 64-char hex (openssl rand -hex 32)
BRIDGE_API_KEY=          # any random secret (openssl rand -hex 16)
```

### 2. Start All Services

```bash
docker compose up
```

This starts: Python backend (`:8000`), Bot service (`:3001`), React frontend (`:3000`), Weaviate (`:8080`), Neo4j (`:7474`/`:7687`), MongoDB (`:27017`), Redis (`:6380`).

**First run takes 2–3 minutes** as Docker builds images and databases initialize.

### 3. Open the Dashboard

Navigate to [http://localhost:3000](http://localhost:3000).

- **Mock mode** (default, `ADAPTER_MOCK=true`): Uses fixture data — no Slack credentials needed.
- **Real mode**: Set `ADAPTER_MOCK=false`, add your Slack token, restart, then connect a workspace in Settings → Connections.

### 4. Sync a Channel

In the dashboard: **Connections → Add Workspace → Select channels → Sync**.

Or via API:
```bash
curl -X POST http://localhost:8000/api/channels/C12345/sync
```

---

## Local Development (Without Docker)

Run each service directly if you prefer faster iteration:

### Prerequisites

- Python 3.12+ with [uv](https://docs.astral.sh/uv/)
- Node.js 20+
- Running databases (use Docker for just the infra):

```bash
# Start only the databases
docker compose up weaviate neo4j mongodb redis
```

### Backend

```bash
# Install dependencies
uv sync

# Run the FastAPI server
uv run uvicorn beever_atlas.server.app:app --reload --port 8000
```

### Bot Service

```bash
cd bot
npm install
npm run dev
```

### Frontend

```bash
cd web
npm install
npm run dev        # Starts at http://localhost:5173
```

The frontend dev server proxies API calls to `http://localhost:8000` by default (configured in `VITE_API_URL`).

---

## Environment Variables

All services read from a single `.env` file in the project root.

### Required

| Variable | Description |
|---|---|
| `GOOGLE_API_KEY` | Gemini API key for all LLM operations |
| `JINA_API_KEY` | Jina v4 embeddings |

### Database (defaults work with Docker Compose)

| Variable | Default | Description |
|---|---|---|
| `WEAVIATE_URL` | `http://localhost:8080` | Weaviate instance URL |
| `MONGODB_URI` | `mongodb://localhost:27017/beever_atlas` | MongoDB connection string |
| `REDIS_URL` | `redis://localhost:6379` | Redis URL |
| `NEO4J_URI` | `bolt://localhost:7687` | Neo4j Bolt URI |
| `NEO4J_AUTH` | `neo4j/beever_atlas_dev` | Neo4j `user/password` |

### Bot & Bridge

| Variable | Default | Description |
|---|---|---|
| `BRIDGE_URL` | `http://localhost:3001` | Backend → Bot service URL |
| `BRIDGE_API_KEY` | — | Shared secret for backend↔bot auth |
| `BACKEND_URL` | `http://localhost:8000` | Bot → Backend URL |
| `BOT_PORT` | `3001` | Bot HTTP server port |

### Credentials & Security

| Variable | Description |
|---|---|
| `CREDENTIAL_MASTER_KEY` | 64-char hex key for AES-256-GCM encryption of platform credentials. Required to store Slack/Discord tokens. Generate: `openssl rand -hex 32` |
| `BRIDGE_API_KEY` | Shared HMAC secret for bot bridge auth. Generate: `openssl rand -hex 16` |

### Platform Credentials (added via UI or env)

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` token for Slack workspace |
| `SLACK_SIGNING_SECRET` | Slack app signing secret |
| `ADAPTER_MOCK` | `true` = use fixture data, no platform needed |

### Pipeline Tuning

| Variable | Default | Description |
|---|---|---|
| `SYNC_BATCH_SIZE` | `50` | Messages per ingestion batch |
| `SYNC_MAX_MESSAGES` | `1000` | Max messages per sync run |
| `QUALITY_THRESHOLD` | `0.5` | Minimum fact quality score (0.0–1.0) |
| `ENTITY_THRESHOLD` | `0.6` | Minimum entity confidence (0.0–1.0) |
| `MAX_FACTS_PER_MESSAGE` | `2` | Max atomic facts extracted per message |
| `LLM_FAST_MODEL` | `gemini-2.5-flash` | Model for extraction/classification |
| `LLM_QUALITY_MODEL` | `gemini-2.5-flash` | Model for wiki synthesis |

### Graph Backend

Atlas supports two graph backends:

| Variable | Default | Options |
|---|---|---|
| `GRAPH_BACKEND` | `neo4j` | `neo4j`, `nebula` |

For **NebulaGraph** (alternative to Neo4j):

```bash
pip install ".[nebula]"

GRAPH_BACKEND=nebula
NEBULA_HOSTS=127.0.0.1:9669
NEBULA_USER=root
NEBULA_PASSWORD=nebula
NEBULA_SPACE=beever_atlas
```

Use `docker-compose.nebula.yml` for a NebulaGraph-based stack.

---

## Supported Platforms

| Platform | Ingestion | Real-time Bot | Status |
|---|---|---|---|
| **Slack** | ✅ Full (messages, threads, files) | ✅ Bot mentions | Stable |
| **Discord** | ✅ Full (messages, threads) | 🔧 Partial | Beta |
| **Microsoft Teams** | ✅ Full (via Graph API) | 🔧 Partial | Beta |

To connect a platform:
1. Create a bot/app in the platform's developer portal
2. Add bot credentials in the dashboard: **Settings → Connections → Add**
3. Select channels to monitor, click **Sync**

---

## Project Structure

```
beever-atlas/
├── src/beever_atlas/          # Python backend
│   ├── agents/                # Google ADK agents
│   │   ├── ingestion/         # 6-stage ingestion pipeline
│   │   │   ├── pipeline.py    # SequentialAgent orchestrator
│   │   │   ├── preprocessor.py
│   │   │   ├── fact_extractor.py
│   │   │   ├── entity_extractor.py
│   │   │   ├── embedder.py
│   │   │   └── persister.py
│   │   └── query/             # Q&A routing agents
│   ├── api/                   # FastAPI route handlers
│   │   ├── ask.py             # Streaming Q&A endpoint (SSE)
│   │   ├── channels.py        # Channel listing & history
│   │   ├── connections.py     # Platform connection CRUD
│   │   ├── graph.py           # Entity/relationship endpoints
│   │   ├── memories.py        # Fact search & listing
│   │   ├── sync.py            # Sync trigger & status
│   │   └── wiki.py            # Wiki retrieval & refresh
│   ├── services/              # Core business logic
│   │   ├── batch_pipeline.py  # Gemini Batch API orchestrator
│   │   ├── consolidation.py   # Topic clustering + summaries
│   │   ├── media_processor.py # Image/PDF/video processing
│   │   ├── scheduler.py       # Background sync scheduling
│   │   └── sync_runner.py     # Sync job coordinator
│   ├── stores/                # Data store clients
│   │   ├── weaviate_store.py  # Semantic memory (Weaviate)
│   │   ├── neo4j_store.py     # Graph memory (Neo4j)
│   │   ├── nebula_store.py    # Graph memory (NebulaGraph alt.)
│   │   └── mongodb_store.py   # State & wiki cache (MongoDB)
│   ├── wiki/                  # Wiki generation
│   │   ├── builder.py         # Orchestrates full wiki build
│   │   ├── compiler.py        # LLM page generation (WikiCompiler)
│   │   └── cache.py           # MongoDB wiki cache
│   ├── adapters/              # Platform message adapters
│   │   ├── slack_adapter.py
│   │   ├── discord_adapter.py
│   │   └── teams_adapter.py
│   ├── infra/                 # Config, logging, health
│   └── models/                # Pydantic domain models
├── bot/                       # TypeScript bot service
│   ├── src/bridge/            # Platform bridge handlers
│   └── src/adapters/          # Slack/Discord/Teams listeners
├── web/                       # React frontend
│   ├── src/components/wiki/   # Wiki page components
│   ├── src/components/graph/  # Cytoscape graph visualization
│   └── src/hooks/             # TanStack Query data hooks
├── docs/v2/                   # Technical specifications
│   ├── 01-architecture-overview.md
│   ├── 02-semantic-memory.md
│   ├── 03-graph-memory.md
│   ├── 04-query-router.md
│   ├── 05-ingestion-pipeline.md
│   ├── 06-wiki-generation.md
│   ├── 07-deployment.md
│   ├── 08-resilience.md
│   ├── 09-observability.md
│   ├── 10-access-control.md
│   ├── 11-frontend-design.md
│   ├── 12-api-design.md
│   └── 13-adk-integration.md
├── docker-compose.yml         # Full stack (Neo4j)
├── docker-compose.nebula.yml  # Full stack (NebulaGraph)
├── .env.example
└── pyproject.toml
```

---

## API Reference

The backend exposes a REST API at `http://localhost:8000`. Interactive docs at `/docs` (Swagger UI).

### Key Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/health` | Health check (all components) |
| `GET` | `/api/channels` | List synced channels |
| `POST` | `/api/channels/{id}/sync` | Trigger channel sync |
| `GET` | `/api/channels/{id}/sync/status` | Poll sync progress |
| `POST` | `/api/channels/{id}/ask` | Streaming Q&A (SSE) |
| `GET` | `/api/channels/{id}/wiki` | Get cached wiki |
| `POST` | `/api/channels/{id}/wiki/refresh` | Trigger wiki regeneration |
| `GET` | `/api/channels/{id}/wiki/structure` | Sidebar navigation tree |
| `GET` | `/api/channels/{id}/wiki/pages/{page_id}` | Single wiki page |
| `GET` | `/api/channels/{id}/wiki/download` | Export wiki as Markdown |
| `GET` | `/api/graph/entities` | List knowledge graph entities |
| `GET` | `/api/graph/entities/{id}/neighbors` | N-hop neighborhood |
| `GET` | `/api/connections` | List platform connections |
| `POST` | `/api/connections` | Add platform connection |
| `DELETE` | `/api/connections/{id}` | Remove connection |

### Ask Endpoint (Streaming SSE)

```bash
curl -N -X POST http://localhost:8000/api/channels/C12345/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "What did we decide about authentication?"}'
```

Response is a stream of `event: type\ndata: {...}` events:
- `thinking` — agent reasoning trace
- `tool_call` — each store query as it happens
- `response_delta` — answer tokens
- `citations` — source messages with permalinks
- `metadata` — route used, cost, confidence
- `done` — stream complete

---

## Cost Model

All LLM costs go through your own API keys. Atlas is designed to minimize LLM calls:

| Operation | Approximate Cost |
|---|---|
| Sync (text message) | ~$0.0025 / message |
| Sync (with image) | ~$0.008 / message |
| Sync (with PDF) | ~$0.004 / message |
| Q&A (semantic route) | ~$0.001 / query |
| Q&A (graph route) | ~$0.005 / query |
| Wiki regeneration | ~$0.03–0.08 / channel |
| Wiki reads (cached) | FREE |

Wiki reads are free because the wiki is pre-generated and cached in MongoDB. LLM cost is only incurred on first generation and when the `wiki_dirty` flag is set (after new data arrives).

---

## Development

### Running Tests

```bash
uv run pytest tests/
```

### Mock Mode

For development without platform credentials, set `ADAPTER_MOCK=true` in `.env`. The system uses fixture data from `src/beever_atlas/adapters/fixtures/`.

### Adding a New Platform Adapter

1. Implement `BaseAdapter` in `src/beever_atlas/adapters/`:
    ```python
    class MyPlatformAdapter(BaseAdapter):
        async def fetch_history(self, channel_id, since=None, limit=500) -> list[NormalizedMessage]: ...
        async def list_channels(self) -> list[ChannelInfo]: ...
    ```
2. Register it in `src/beever_atlas/adapters/__init__.py`
3. Add a corresponding bridge handler in `bot/src/adapters/`

### Health Check

```bash
curl http://localhost:8000/api/health
```

```json
{
  "status": "healthy",
  "components": {
    "weaviate": {"status": "up", "latency_ms": 12},
    "neo4j": {"status": "up", "latency_ms": 8},
    "mongodb": {"status": "up", "latency_ms": 3},
    "redis": {"status": "up", "latency_ms": 1}
  },
  "checked_at": "2025-04-08T12:00:00Z"
}
```

---

## Troubleshooting

### Bot service unreachable (503 on sync)

The Python backend calls the bot service to list channels and register adapters. Make sure the bot is running and `BRIDGE_URL` points to it.

```bash
# Check bot health
curl http://localhost:3001/health
```

### Wiki not generating

Check that consolidation ran after sync. The wiki is generated from topic clusters — if no clusters exist, the wiki will be empty. Check logs:

```bash
docker compose logs beever-atlas | grep consolidation
```

### Weaviate schema errors on startup

If you see schema errors, reset Weaviate's data volume:

```bash
docker compose down -v  # WARNING: deletes all indexed data
docker compose up
```

### CREDENTIAL_MASTER_KEY not set

Platform credentials (Slack tokens, etc.) cannot be stored without this key. Set it in `.env`:

```bash
echo "CREDENTIAL_MASTER_KEY=$(openssl rand -hex 32)" >> .env
```

---

## Documentation

Full technical specifications live in [`docs/v2/`](docs/v2/):

| Doc | Contents |
|---|---|
| [`01-architecture-overview.md`](docs/v2/01-architecture-overview.md) | System design, dual-memory architecture, design principles |
| [`02-semantic-memory.md`](docs/v2/02-semantic-memory.md) | Weaviate schema, 3-tier hierarchy, retrieval strategies |
| [`03-graph-memory.md`](docs/v2/03-graph-memory.md) | Neo4j schema, entity types, relationship model |
| [`04-query-router.md`](docs/v2/04-query-router.md) | LLM-powered routing, cost optimization |
| [`05-ingestion-pipeline.md`](docs/v2/05-ingestion-pipeline.md) | 6-stage pipeline, quality gates, entity extraction |
| [`06-wiki-generation.md`](docs/v2/06-wiki-generation.md) | Wiki page types, rendering stack, generation flow |
| [`07-deployment.md`](docs/v2/07-deployment.md) | Docker Compose, production setup |
| [`08-resilience.md`](docs/v2/08-resilience.md) | Circuit breakers, LLM fallback, outbox pattern |
| [`09-observability.md`](docs/v2/09-observability.md) | Health checks, metrics, distributed tracing |
| [`10-access-control.md`](docs/v2/10-access-control.md) | Channel ACL, private channel filtering |
| [`11-frontend-design.md`](docs/v2/11-frontend-design.md) | React dashboard architecture |
| [`12-api-design.md`](docs/v2/12-api-design.md) | Full REST API + response schemas |
| [`13-adk-integration.md`](docs/v2/13-adk-integration.md) | Google ADK agent hierarchy, tools, callbacks |

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feat/your-feature`
3. Make your changes and add tests
4. Run tests: `uv run pytest`
5. Open a pull request

Please open an issue first for significant changes.

---

## License

[MIT](LICENSE)

---

<div align="center">
Built with ❤️ using <a href="https://google.github.io/adk-docs/">Google ADK</a>, <a href="https://weaviate.io">Weaviate</a>, <a href="https://neo4j.com">Neo4j</a>, and <a href="https://fastapi.tiangolo.com">FastAPI</a>
</div>
