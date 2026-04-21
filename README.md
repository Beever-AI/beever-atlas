<div align="center">

<img src="web/public/logo-primary.svg" alt="Beever Atlas" height="120" />

# Beever Atlas

**Turn team conversations into a living, searchable knowledge base — automatically.**

[![CI](https://img.shields.io/github/actions/workflow/status/beever-ai/beever-atlas/ci.yml?branch=main&label=CI)](https://github.com/beever-ai/beever-atlas/actions)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://www.apache.org/licenses/LICENSE-2.0)
[![Latest version](https://img.shields.io/github/v/release/beever-ai/beever-atlas?label=version)](https://github.com/beever-ai/beever-atlas/releases)
[![Star History](https://img.shields.io/github/stars/beever-ai/beever-atlas?style=social)](https://github.com/beever-ai/beever-atlas/stargazers)

</div>

---

## Quick Start

Beever Atlas ships as a Docker Compose stack (backend + bot + web + 4 datastores). You can try a seeded demo in 30 seconds with zero keys, then pick one of **three deployment options** to install it for real.

### 1. Get the code

```bash
git clone https://github.com/beever-ai/beever-atlas.git
cd beever-atlas
```

### 2. Try the demo first (optional, no keys needed for seeding)

```bash
make demo
```

`make demo` brings up the full stack pre-loaded with a public Wikipedia corpus (Ada Lovelace + Python history). Seeding uses pre-computed fixtures — no API keys required. Asking questions via `/api/ask` needs a free-tier `GOOGLE_API_KEY` because the QA agent calls Gemini. See [demo/README.md](demo/README.md) for curl examples.

Skip this step if you're ready to install for real.

### 3. Before you start: get your API keys

Two free keys are required before installing. Both offer generous free tiers — enough to sync a small team's channels for testing.

| Key | Purpose | Where to get it |
|---|---|---|
| `GOOGLE_API_KEY` | Gemini — extraction, entity graph, answers | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |
| `JINA_API_KEY` | Jina v4 embeddings (2048-dim) for semantic search | [jina.ai/api-dashboard](https://jina.ai/api-dashboard/) |

Optional (skip unless you know you need them):

| Key | What it enables |
|---|---|
| `TAVILY_API_KEY` | External web search when QA retrieval confidence is low — [tavily.com](https://tavily.com/) |
| Slack / Discord / Teams bot tokens | **Configured via the web UI after setup**, not `.env` — the bot stores platform credentials encrypted in MongoDB |

> **Tip:** Keep the two required keys handy before you start. Option 1 prompts for them interactively; Options 2 and 3 need them pasted into `.env`.

### 4. Choose a deployment option

| Option | When to use | Time to "up" |
|---|---|---|
| **1. One-line install** (recommended) | You want the fastest path to a running stack. | ~2 min first run |
| **2. Manual Docker** | CI/CD, ops environments, or when you want explicit control over every step. | ~3 min first run |
| **3. Local development** | Active contributors who need hot-reload on backend and frontend. | varies |

#### Option 1 — One-line install (recommended)

```bash
./atlas
```

The `atlas` installer walks you through a guided 4-step checklist:

1. **Required LLM keys** — prompts for `GOOGLE_API_KEY` (Gemini) and `JINA_API_KEY` (embeddings); press Enter to skip either.
2. **Optional integrations** — Tavily web search, Ollama, MCP server for Claude Code / Cursor.
3. **Graph backend** — Neo4j (default) or skip.
4. **Auth tokens** — keep dev defaults or rotate now.

Under the hood it verifies `docker` + `docker compose`, copies `.env.example` → `.env` (preserves your values on re-run, `chmod 600`), auto-generates `CREDENTIAL_MASTER_KEY` (64 hex) and `WEAVIATE_API_KEY` (32 hex), runs a port-conflict preflight, launches the stack via `docker compose up -d --build --force-recreate --remove-orphans`, and polls `/api/health` before printing the ready card.

When you see **"Beever Atlas is ready"**, open **[http://localhost:3000](http://localhost:3000)**.

For CI or unattended installs — skip prompts, pre-seed keys from shell env:

```bash
GOOGLE_API_KEY=... JINA_API_KEY=... ./atlas --non-interactive
```

Re-running `./atlas` on an existing stack is idempotent.

#### Option 2 — Manual Docker

Full control, step-by-step.

```bash
cp .env.example .env
```

Open `.env` and fill in the two required keys:

```env
GOOGLE_API_KEY=your_gemini_key
JINA_API_KEY=your_jina_key
```

Generate two required secrets and paste them into `.env`:

```bash
# CREDENTIAL_MASTER_KEY — AES-256-GCM key for stored platform credentials (64 hex chars)
python -c "import secrets; print(secrets.token_hex(32))"

# WEAVIATE_API_KEY — auth between backend and Weaviate (required by docker-compose)
python -c "import secrets; print(secrets.token_hex(16))"
```

Launch:

```bash
docker compose up -d --build
```

Open **[http://localhost:3000](http://localhost:3000)**.

**Services started:**

| Service | Port | Description |
|---|---|---|
| Web (nginx) | `:3000` | React dashboard |
| Backend | `:8000` | FastAPI + ADK agents |
| Bot | `:3001` | Platform bridge (Slack / Discord / Teams) |
| Weaviate | `:8080` | Semantic memory |
| Neo4j | `:7474` / `:7687` | Graph memory |
| MongoDB | `:27017` | State + wiki cache |
| Redis | `:6380` | Sessions (internal `:6379`) |

First run takes 2–3 minutes while images build and databases initialize. Subsequent runs start in seconds.

#### Option 3 — Local development

Databases in Docker, app services native for hot-reload.

**Prerequisites:** Python 3.12+ with [uv](https://docs.astral.sh/uv/), Node.js 20+

```bash
cp .env.example .env
# Fill in GOOGLE_API_KEY, JINA_API_KEY, CREDENTIAL_MASTER_KEY, WEAVIATE_API_KEY (same as Option 2)

# Start just the databases
docker compose up -d weaviate neo4j mongodb redis

# Backend (terminal 1)
uv sync
uv run uvicorn beever_atlas.server.app:app --reload --port 8000

# Bot (terminal 2)
cd bot && npm install && npm run dev

# Web (terminal 3) — Vite dev server with HMR
cd web && npm install && npm run dev
```

Open **[http://localhost:5173](http://localhost:5173)** (the Vite dev port — **not** `:3000`).

The Vite dev server proxies `/api/*` to `http://localhost:8000` (configured via `VITE_API_URL`).

### 5. Open the dashboard

Navigate to the URL for your chosen option:

- **Options 1 & 2** → **[http://localhost:3000](http://localhost:3000)**
- **Option 3** → **[http://localhost:5173](http://localhost:5173)**

From there:

- **Real mode** (default, `ADAPTER_MOCK=false`): connect a workspace in **Settings → Connections** — Slack / Discord / Teams tokens are entered through the UI, not `.env`.
- **Mock mode** (`ADAPTER_MOCK=true`): uses fixture data — opt in for local UI iteration without platform credentials.

### 6. Sync a channel

From the dashboard: **Connections → Add Workspace → Select channels → Sync**.

Or via API (auto-extracts your bearer token from `.env`):

```bash
curl -X POST http://localhost:8000/api/channels/C12345/sync \
  -H "Authorization: Bearer $(grep -E '^BEEVER_API_KEYS=' .env | cut -d= -f2 | cut -d, -f1)"
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

## License

[Apache License 2.0](LICENSE) © 2026 Beever Atlas contributors. Third-party attributions in [NOTICE](NOTICE).

Security policy: [SECURITY.md](SECURITY.md) | Community standards: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md)
