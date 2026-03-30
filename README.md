# Beever Atlas v2

Wiki-first RAG system with dual semantic + graph memory for Slack, Teams, and Discord.

Beever Atlas ingests messages from communication platforms, builds a persistent knowledge base using two complementary memory systems, and surfaces grounded answers with citations.

- **Semantic Memory** (Weaviate) — 3-tier hierarchical memory for factual and topic queries (~80% of queries)
- **Graph Memory** (Neo4j) — Knowledge graph for relational and temporal queries (~20% of queries)
- **Smart Router** — LLM-powered query understanding that routes to the right memory system

## Architecture

```
┌───────────────────────────────────────────────────────────────┐
│                      BEEVER ATLAS v2                          │
│                                                               │
│  Bot Service (TypeScript)          Python Backend (FastAPI)    │
│  ┌─────────────────────┐          ┌──────────────────────┐   │
│  │ Chat SDK             │          │ ADK Agents           │   │
│  │ ├── @chat-adapter/   │ /bridge/ │ SSE Ask Endpoint     │   │
│  │ │   slack            │◄────────│ Channels API         │   │
│  │ │   teams (future)   │  httpx   │ ChatBridgeAdapter    │   │
│  │ │   discord (future) │          │                      │   │
│  │ └── state-redis      │          │ Ingestion Pipeline   │   │
│  │                      │          │ (M3+)                │   │
│  │ Real-time bot +      │          └──────────┬───────────┘   │
│  │ Bridge REST API      │                     │               │
│  └──────────┬───────────┘              Writes to BOTH         │
│             │                        ┌────────┴────────┐      │
│       Slack/Teams/Discord            ▼                ▼       │
│       (webhooks)                Weaviate           Neo4j       │
│                                (Semantic)         (Graph)      │
│                                                               │
│  Frontend: React 19 + Vite + TailwindCSS + shadcn/ui          │
└───────────────────────────────────────────────────────────────┘
```

**Key design**: The bot service is the **single gateway** to all chat platforms via Chat SDK adapters. The Python backend never talks to Slack/Teams/Discord directly — it calls the bot's bridge API. Adding a new platform = adding one `@chat-adapter/X` package to the bot.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Agent Framework | Google ADK (Python) |
| Chat Bot | Vercel Chat SDK (TypeScript) with `@chat-adapter/slack` |
| Backend API | FastAPI |
| Semantic Store | Weaviate 1.28 |
| Graph Store | Neo4j 5.26 + APOC |
| State Store | MongoDB 7.0 |
| Session Store | Redis 7 |
| Embeddings | Jina v4 (2048-dim) |
| LLM (fast) | Gemini 2.0 Flash Lite, fallback: Claude Haiku 4.5 |
| LLM (quality) | Gemini 2.0 Flash, fallback: Claude Sonnet 4.6 |
| Frontend | React 19 + Vite + TailwindCSS + shadcn/ui |

## Prerequisites

- [Python 3.11+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Node.js 22+](https://nodejs.org/)
- Redis (via `brew install redis` or Docker)
- [Docker](https://www.docker.com/) (optional — only needed for Weaviate, Neo4j, MongoDB)

## Quick Start

### 1. Clone and install dependencies

```bash
git clone <repo-url>
cd beever-atlas-v2

# Python backend
uv sync --extra dev

# React frontend
cd web && npm install && cd ..

# Bot service
cd bot && npm install && cd ..
```

### 2. Configure environment

```bash
cp .env.example .env
```

The `.env` file is shared by all services. Key variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `ADAPTER_MOCK` | No | Set to `true` for mock data (no Slack needed) |
| `SLACK_BOT_TOKEN` | For Slack | Bot token from api.slack.com (`xoxb-...`) |
| `SLACK_SIGNING_SECRET` | For Slack | App signing secret |
| `GOOGLE_API_KEY` | For LLM | Gemini models for ADK agents |
| `REDIS_URL` | Yes | Redis connection (default: `redis://localhost:6379`) |

See `.env.example` for all options.

### 3. Start services for local development

**Start Redis** (required for bot state):
```bash
brew services start redis
# Or: docker compose up -d redis
```

**Start all services** (4 terminals):

```bash
# Terminal 1 — Python Backend (port 8000)
uv run uvicorn beever_atlas.server.app:app --reload --port 8000

# Terminal 2 — Bot Service (port 3001)
cd bot && npm run dev

# Terminal 3 — React Frontend (port 5173)
cd web && npm run dev
```

All services read from the root `.env` file automatically.

### 4. Verify everything works

```bash
# Backend health
curl http://localhost:8000/api/health

# Bot health
curl http://localhost:3001/health

# List channels (mock data)
curl http://localhost:8000/api/channels

# Get channel messages
curl "http://localhost:8000/api/channels/C_MOCK_GENERAL/messages?limit=5"

# Ask endpoint (SSE streaming — needs GOOGLE_API_KEY for real LLM)
curl -N -X POST http://localhost:8000/api/channels/C_MOCK_GENERAL/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "what is our tech stack?"}'
```

**Open the dashboard**: http://localhost:5173

- Navigate to a channel from the sidebar
- **Ask tab** — type a question, see streaming response
- **Messages tab** — browse mock conversation history
- **Wiki tab** — placeholder (coming in M3)

### 5. Run with Docker Compose (full stack)

```bash
docker compose up --build
```

This starts all 7 services:

| Service | Port | Description |
|---------|------|-------------|
| `beever-atlas` | 8000 | FastAPI backend |
| `web` | 3000 | React frontend (nginx) |
| `bot` | 3001 | Chat SDK bot + bridge API |
| `weaviate` | 8080 | Semantic memory |
| `neo4j` | 7474 / 7687 | Graph memory |
| `mongodb` | 27017 | State + wiki cache |
| `redis` | 6379 | Session store |

## Mock Mode vs Real Slack

| Mode | How to enable | What works |
|------|--------------|------------|
| **Mock** (default) | `ADAPTER_MOCK=true` in `.env` | All API endpoints, dashboard, messages — using fixture data (8 users, 2 channels, 63 messages) |
| **Real Slack** | Set `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, remove `ADAPTER_MOCK` | Real Slack integration — bot responds to @mentions, real message history |

See [`docs/guides/slack-setup.md`](docs/guides/slack-setup.md) for Slack app configuration.

## Project Structure

```
beever-atlas-v2/
├── src/beever_atlas/          # Python backend
│   ├── agents/                # ADK agents (echo agent → real agents in M3+)
│   ├── adapters/              # ChatBridgeAdapter + MockAdapter
│   ├── api/                   # REST endpoints (ask, channels)
│   ├── pipeline/              # 7-stage ingestion pipeline (M3)
│   ├── stores/                # Weaviate, Neo4j, MongoDB clients (M3+)
│   ├── retrieval/             # Query routing + retrieval (M3+)
│   ├── wiki/                  # Wiki generation + cache (M5)
│   ├── server/                # FastAPI app
│   └── infra/                 # Config, health, LiteLLM routing
├── bot/                       # Chat SDK bot service (TypeScript)
│   └── src/
│       ├── index.ts           # Bot handlers (onNewMention, onSubscribedMessage)
│       ├── bridge.ts          # Bridge REST API for Python backend
│       ├── formatter.ts       # Slack Block Kit response formatter
│       └── sse-client.ts      # SSE stream consumer
├── web/                       # React frontend (Vite + TailwindCSS)
│   └── src/
│       ├── components/        # UI components (channel/, layout/, memories/)
│       ├── pages/             # Route pages
│       ├── hooks/             # React hooks (useAsk, useMemories)
│       └── lib/               # API client, types, mock data
├── tests/                     # Python test suite
│   ├── fixtures/              # Mock conversation data (JSON)
│   └── test_*.py              # Adapter, endpoint, agent, health tests
├── docs/
│   ├── v2/                    # Architecture and design specs
│   └── guides/                # Setup guides (Slack, etc.)
├── docker-compose.yml         # Full stack orchestration
├── pyproject.toml             # Python dependencies
└── .env                       # Environment configuration (all services)
```

## Running Tests

```bash
# Python tests (122 tests)
uv run python -m pytest tests/ -v

# Bot tests (7 tests)
cd bot && npm test

# Frontend type check
cd web && npx tsc --noEmit

# Frontend build
cd web && npm run build
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/api/health` | System health check |
| `GET` | `/api/channels` | List all channels |
| `GET` | `/api/channels/:id` | Channel metadata |
| `GET` | `/api/channels/:id/messages` | Channel message history |
| `POST` | `/api/channels/:id/ask` | Streaming Q&A (SSE) |

## Milestones

| Milestone | Status | Description |
|-----------|--------|-------------|
| M1: Skeleton & Health | Done | Project scaffold, health checks, Docker |
| M2: Chat Bot + Echo Query | Done | Chat SDK bot, SSE streaming, adapter layer, React workspace |
| M3: Ingest & Store | Next | Weaviate ingestion, batch history processing |
| M4: Graph Memory + Router | — | Neo4j entities, smart query routing |
| M5: Wiki & Tiers | — | Wiki generation, tier consolidation |
| M6: Contradictions & Polish | — | Retrieval quality, contradiction detection |
| M7: Resilience & ACL | — | Circuit breakers, access control |
| M8: Multi-Platform & Polish | — | Teams, Discord adapters, production readiness |

## Documentation

Detailed architecture and design docs are in [`docs/v2/`](docs/v2/README.md):

1. [Architecture Overview](docs/v2/01-architecture-overview.md)
2. [Semantic Memory](docs/v2/02-semantic-memory.md)
3. [Graph Memory](docs/v2/03-graph-memory.md)
4. [Query Router](docs/v2/04-query-router.md)
5. [Ingestion Pipeline](docs/v2/05-ingestion-pipeline.md)
6. [Wiki Generation](docs/v2/06-wiki-generation.md)
7. [Deployment](docs/v2/07-deployment.md)
8. [Resilience](docs/v2/08-resilience.md)
9. [Observability](docs/v2/09-observability.md)
10. [Access Control](docs/v2/10-access-control.md)
11. [Frontend Design](docs/v2/11-frontend-design.md)
12. [API Design](docs/v2/12-api-design.md)
13. [ADK Integration](docs/v2/13-adk-integration.md)

## License

Proprietary.
