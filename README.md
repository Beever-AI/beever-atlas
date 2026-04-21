<div align="center">

# Beever Atlas

**Turn team conversations into a living, searchable knowledge base — automatically.**

<!-- TODO: hero GIF/screenshot — track under separate ticket -->

[![CI](https://img.shields.io/github/actions/workflow/status/beever-ai/beever-atlas/ci.yml?branch=main&label=CI)](https://github.com/beever-ai/beever-atlas/actions)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Latest version](https://img.shields.io/github/v/release/beever-ai/beever-atlas?label=version)](https://github.com/beever-ai/beever-atlas/releases)
[![Star History](https://img.shields.io/github/stars/beever-ai/beever-atlas?style=social)](https://github.com/beever-ai/beever-atlas/stargazers)

</div>

---

## Quick Start

### 1. Get the code

```bash
git clone https://github.com/beever-ai/beever-atlas.git
cd beever-atlas
```

### 2a. Try the demo in 30 seconds (zero API keys for seeding)

```bash
make demo
```

`make demo` brings up the full stack pre-loaded with a public Wikipedia corpus (Ada Lovelace + Python history). Seeding uses pre-computed fixtures — no API keys needed. Asking questions via `/api/ask` requires a free-tier `GOOGLE_API_KEY` because the QA agent calls Gemini. See [demo/README.md](demo/README.md) for curl examples.

### 2b. Full setup (real LLM + platform connections)

```bash
cp .env.example .env
```

Open `.env` and fill in these **required** keys:

```env
# LLM + embeddings (required)
GOOGLE_API_KEY=your_gemini_key
JINA_API_KEY=your_jina_key

# API authentication (any strings — tokens clients must send as Bearer)
BEEVER_API_KEYS=dev-key-change-me
BEEVER_ADMIN_TOKEN=dev-admin-change-me

# Database passwords (NEO4J_PASSWORD must match the password half of NEO4J_AUTH)
NEO4J_PASSWORD=beever_atlas_dev
WEAVIATE_API_KEY=any-long-random-string

# Bridge auth + credential encryption
BRIDGE_API_KEY=$(openssl rand -hex 16)
CREDENTIAL_MASTER_KEY=$(openssl rand -hex 32)
```

> **Tip:** `CREDENTIAL_MASTER_KEY` must be exactly 64 hex chars (AES-256-GCM). Setting `BEEVER_ENV=production` makes startup fail if any of the above are defaults.

### 3. Start the full stack

```bash
docker compose up
```

This builds and starts everything in one command:

| Service | Port | Description |
|---|---|---|
| React frontend | `:3000` | Dashboard UI |
| Python backend | `:8000` | FastAPI + ADK agents |
| Bot service | `:3001` | Platform bridge (Slack / Discord / Teams) |
| Weaviate | `:8080` | Semantic memory |
| Neo4j | `:7474` / `:7687` | Graph memory |
| MongoDB | `:27017` | State + wiki cache |
| Redis | `:6379` | Sessions |

First run takes 2–3 minutes while images build and databases initialize. Subsequent runs start in seconds.

### 4. Open the dashboard

Navigate to **[http://localhost:3000](http://localhost:3000)**.

- **Mock mode** (default, `ADAPTER_MOCK=true`): uses fixture data — no platform credentials required.
- **Real mode**: set `ADAPTER_MOCK=false`, then connect a workspace in **Settings → Connections** (Slack / Discord / Teams tokens are entered through the UI, not `.env`).

### 5. Sync a channel

From the dashboard: **Connections → Add Workspace → Select channels → Sync**.

Or via API:

```bash
curl -X POST http://localhost:8000/api/channels/C12345/sync \
  -H "Authorization: Bearer dev-key-change-me"
```

### MCP server (for external AI agents)

Beever Atlas exposes a curated MCP (Model Context Protocol) server at `/mcp` for AI agents like Claude Code and Cursor. This allows external code assistants to query your team's knowledge base without using the dashboard.

See [docs/mcp-server.md](docs/mcp-server.md) for:
- **Tool catalog** — 16 tools for discovery, retrieval, graph traversal, and long-running operations
- **Auth setup** — generating and managing `BEEVER_MCP_API_KEYS`
- **Client configuration** — ready-to-use `.mcp.json` templates for Claude Code and Cursor
- **Rate limits** — principal-keyed limits to prevent one agent from throttling others

Quick example (Claude Code):
```json
{
  "mcpServers": {
    "beever-atlas": {
      "url": "https://atlas.example.com/mcp",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer ${BEEVER_MCP_KEY}"
      }
    }
  }
}
```

### Common commands

```bash
docker compose up -d              # Start in background
docker compose logs -f beever-atlas   # Tail backend logs
docker compose down               # Stop (keeps data)
docker compose down -v            # Stop and DELETE all indexed data
make demo                         # Full stack + seeded demo corpus
make docker-up                    # Shortcut for `docker compose up -d`
```

---

## Live Demo

Live demo — coming in a future release. For now, run `make demo` locally via the Quick Start above.

---

## Architecture

Beever Atlas runs three services (backend, bot, frontend) backed by four data stores (Weaviate, Neo4j, MongoDB, Redis). Knowledge is held in two complementary memory systems: a semantic store for fast vector search and a graph store for relationship traversal.

See the [architecture overview](https://docs.beever.ai/atlas/concepts/architecture) on the documentation site for the full design, including component responsibilities, dual-memory internals, and the smart query router.

---

## Why Wiki-First RAG?

Most RAG systems answer questions by retrieving raw message snippets and feeding them straight to an LLM. Beever Atlas takes a different approach: it continuously distils conversations into a structured, auto-maintained wiki — with topic pages, entity graphs, decisions, and citations — before any query is issued. When you ask a question, the retrieval layer works against clean, deduplicated knowledge rather than noisy chat history. This means answers are more consistent, citations are traceable to source messages, and the wiki itself becomes a useful artifact your team can browse independently of the Q&A interface. The dual-memory architecture (semantic + graph) lets the query router pick the right retrieval strategy per question, keeping latency low and context precise.

For a detailed comparison with other LLM knowledge tools, see [the comparison page](https://docs.beever.ai/atlas/comparison) on the documentation site.

---

## Features

- **Multi-platform ingestion** — Slack, Discord, and Microsoft Teams supported out of the box.
- **Dual semantic + graph memory** — Weaviate for hybrid BM25 + vector search; Neo4j for entity and relationship traversal.
- **Auto-generated wiki** — hierarchical topic pages with Mermaid diagrams and source citations, cached in MongoDB.
- **Smart query routing** — LLM-powered router picks semantic or graph retrieval per question.
- **Wiki consolidation** — atomic facts are clustered by cosine similarity into browsable topic groups, no manual curation required.
- **Self-hosted** — all data stays in databases you control; no telemetry is sent anywhere.

---

## Privacy & Telemetry

Beever Atlas collects no telemetry. No usage data, error reports, or analytics are sent anywhere by default. All LLM calls go through API keys you configure in your own `.env`, and all data stays in the databases you control.

---

## API Stability

All `/api/*` endpoints are **UNSTABLE** in 0.1.0. v0.2.0 will introduce a `/api/v1/*` prefix; clients pinning current paths will break. See [SECURITY.md](SECURITY.md).

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

---

## License

[Apache License 2.0](LICENSE) © 2026 Beever Atlas contributors. Third-party attributions in [NOTICE](NOTICE).

Security policy: [SECURITY.md](SECURITY.md) | Community standards: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
