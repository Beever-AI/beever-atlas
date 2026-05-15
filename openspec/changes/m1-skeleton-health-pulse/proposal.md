## Why

Beever Atlas v2 needs its foundational project skeleton before any feature work can begin. The v2 repo currently contains only documentation (`docs/v2/`) — no source code, no infrastructure, no services. M1 establishes the Docker Compose stack (7 services), Python backend shell, React frontend shell, TypeScript bot placeholder, and a health endpoint proving everything is connected. This is the "walking skeleton" that all subsequent milestones (M2-M8) build on.

## What Changes

- Create `src/beever_atlas/` Python package with module directories for agents, adapters, pipeline, stores, retrieval, wiki, server, and infra
- Add config system loading env vars for all 7 dependencies (Weaviate, Neo4j, MongoDB, Redis) and API keys (Gemini, Jina, Tavily, Anthropic) with LiteLLM model routing
- Scaffold ADK agent foundation: `tools.py` with FunctionTool stubs, LiteLLM config, Runner + InMemorySessionService integration
- Docker Compose with Weaviate, Neo4j, MongoDB, Redis, FastAPI backend, React frontend, bot service
- FastAPI app shell with `GET /api/health` checking all 4 data stores, CORS for React dev server
- React 19 + Vite + TailwindCSS + shadcn/ui frontend with layout shell, route stubs, HealthBadge, dashboard home
- React Memories tab: 3-tier memory browser with TierBrowser, SummaryCard, ClusterCard, FactCard components (mock data for M1)
- TypeScript bot service placeholder connecting to Redis
- `.env.example` with all required env vars documented

## Capabilities

### New Capabilities
- `project-scaffold`: Python package structure, Docker Compose, config system, env var management
- `adk-foundation`: ADK agent scaffolding — Runner, session service, LiteLLM config, FunctionTool stubs
- `health-endpoint`: FastAPI shell with GET /api/health, DependencyHealth registry, CORS
- `frontend-shell`: React 19 + Vite + TailwindCSS + shadcn/ui layout, routing, HealthBadge, dashboard
- `memories-browser`: 3-tier memory browser UI (Tier 0 summary, Tier 1 clusters, Tier 2 facts)
- `bot-placeholder`: TypeScript bot service with Redis connection, Docker integration

### Modified Capabilities
<!-- None — greenfield project -->

## Impact

- **New files**: ~40-60 files across `src/`, `web/`, `bot/`, root configs
- **Dependencies**: Python (FastAPI, google-adk, litellm, weaviate-client, neo4j, pymongo, redis), Node.js (React 19, Vite, TailwindCSS, shadcn/ui, React Router v7)
- **Infrastructure**: Docker Compose defining 7 services with networking
- **APIs**: `GET /api/health` — first REST endpoint
- **No breaking changes** — greenfield project initialization
