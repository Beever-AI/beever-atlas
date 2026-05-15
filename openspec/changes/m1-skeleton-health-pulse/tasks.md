## 1. Project Scaffold (RES-69)

- [x] 1.1 Create `pyproject.toml` with uv, all Python dependencies (FastAPI, google-adk, litellm, weaviate-client, neo4j, pymongo, redis, pydantic, uvicorn, pytest)
- [x] 1.2 Create `src/beever_atlas/__init__.py` and all submodule directories with `__init__.py` (agents, adapters, pipeline, stores, retrieval, wiki, server, infra)
- [x] 1.3 Create `.env.example` with all required env vars
- [x] 1.4 Create `docker-compose.yml` with all 7 services (weaviate, neo4j, mongodb, redis, backend, frontend, bot) and health checks
- [x] 1.5 Create backend `Dockerfile`
- [x] 1.6 Write tests: verify package imports, verify all submodules exist

## 2. Config System (RES-71)

- [x] 2.1 Implement `src/beever_atlas/infra/config.py` — Pydantic Settings class loading all env vars with validation
- [x] 2.2 Implement `src/beever_atlas/infra/litellm_config.py` — model routing for fast/quality tiers
- [x] 2.3 Write tests: config loads from env, missing vars raise error, model tier resolution

## 3. ADK Agent Scaffolding (RES-91)

- [x] 3.1 Implement `src/beever_atlas/agents/tools.py` — 11 FunctionTool stubs with correct signatures, docstrings, raising NotImplementedError
- [x] 3.2 Implement `src/beever_atlas/agents/runner.py` — ADK Runner creation with InMemorySessionService, session-per-request pattern
- [x] 3.3 Write tests: all 11 tools importable and raise NotImplementedError, runner creation works

## 4. FastAPI App Shell + Health Endpoint (RES-98)

- [x] 4.1 Implement `src/beever_atlas/server/app.py` — FastAPI app with CORS middleware
- [x] 4.2 Implement `src/beever_atlas/infra/health.py` — DependencyHealth registry with timeout support
- [x] 4.3 Implement `GET /api/health` endpoint with per-component checks (Weaviate, Neo4j, MongoDB, Redis)
- [x] 4.4 Define Pydantic models: `HealthResponse`, `ComponentHealth`
- [x] 4.5 Write tests: health endpoint returns correct schema, handles service up/down, CORS headers present

## 5. React App Scaffold (RES-99)

- [x] 5.1 Initialize Vite + React 19 + TypeScript project in `web/`
- [x] 5.2 Configure TailwindCSS + shadcn/ui with design tokens (Inter font, slate/indigo palette)
- [x] 5.3 Set up React Router v7 with route stubs (/, /channels, /channels/:id, /search, /graph, /settings)
- [x] 5.4 Build `Sidebar.tsx` with nav links, icons, collapse toggle (240px/64px)
- [x] 5.5 Build `Header.tsx` with page title
- [x] 5.6 Build `HealthBadge.tsx` — polls /api/health every 30s, green/amber/red/gray indicator
- [x] 5.7 Create `lib/api.ts` — fetch wrapper with VITE_API_URL, error handling
- [x] 5.8 Create `lib/types.ts` — TypeScript interfaces matching backend schemas
- [x] 5.9 Build Dashboard page (`/`) with stat card placeholders + HealthBadge
- [x] 5.10 Create web `Dockerfile` for Docker Compose
- [x] 5.11 Write tests: build succeeds, components render without errors

## 6. Chat SDK Bot Placeholder (RES-100)

- [x] 6.1 Initialize `bot/` Node.js TypeScript project with tsconfig
- [x] 6.2 Implement entry point: connect to Redis, log "Bot service ready"
- [x] 6.3 Create `bot/Dockerfile` for Docker Compose
- [x] 6.4 Write tests: service starts, handles Redis connection failure

## 7. React Memories Tab (RES-112)

- [x] 7.1 Create mock data file with MemoryTier0, MemoryTier1, MemoryTier2 (1 summary, 3 clusters, 5+ facts)
- [x] 7.2 Implement `useMemories.ts` hook returning mock data with filter state
- [x] 7.3 Build `SummaryCard.tsx` — Tier 0 channel summary (always visible)
- [x] 7.4 Build `ClusterCard.tsx` — Tier 1 expandable topic clusters
- [x] 7.5 Build `FactCard.tsx` — Tier 2 atomic facts with quality badge, expandable detail
- [x] 7.6 Build `MemoryFilters.tsx` — topic, entity, importance, date range filters
- [x] 7.7 Build `TierBrowser.tsx` — compose all components in 3-tier layout
- [x] 7.8 Add `/channels/:id/memories` route and wire up TierBrowser
- [x] 7.9 Write tests: components render with mock data, filter interactions work
