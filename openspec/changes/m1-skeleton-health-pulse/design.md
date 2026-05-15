## Context

Beever Atlas v2 is a greenfield project with only `docs/v2/` (13 spec documents) currently in the repo. M1 ("Skeleton & Health Pulse") initializes the full-stack project: Python backend, React frontend, TypeScript bot service, Docker Compose infrastructure, and a health endpoint proving connectivity. All subsequent milestones build on this skeleton.

The Linear milestone defines 8 active tasks (RES-90 already done) with clear spec references to `docs/v2/`. The tasks have a natural dependency order: package structure → config → ADK scaffolding → FastAPI shell → React shell → bot placeholder → memories tab.

## Goals / Non-Goals

**Goals:**
- Establish `src/beever_atlas/` Python package with all module directories matching `docs/v2/07-deployment.md`
- Config system loading all env vars for 7 dependencies + API keys + LiteLLM model routing
- ADK agent foundation: FunctionTool stubs, LiteLLM config, Runner + session service pattern
- FastAPI app with `GET /api/health` checking Weaviate, Neo4j, MongoDB, Redis connectivity
- React 19 + Vite + TailwindCSS + shadcn/ui with layout shell, route stubs, dashboard, HealthBadge
- 3-tier memory browser UI with mock data
- TypeScript bot service placeholder with Redis connection
- Docker Compose orchestrating all 7 services
- Tests for each component

**Non-Goals:**
- No actual Slack/Teams/Discord adapters (M2)
- No real ingestion pipeline (M3)
- No graph memory implementation (M4)
- No wiki generation (M5)
- No circuit breaker degradation logic (M7) — only health check portion of DependencyHealth
- No authentication/ACL (M7)
- No real data in memory browser — mock/placeholder data only

## Decisions

### 1. Python project tooling: uv + pyproject.toml
Use `uv` as the Python package manager with `pyproject.toml` for dependency management. Modern, fast, and replaces pip/poetry.
- **Alternative**: Poetry — heavier, slower resolution, less momentum
- **Alternative**: pip + requirements.txt — no lock file, less reproducible

### 2. Package layout: src/ layout
Use `src/beever_atlas/` (src layout) per Python packaging best practices and `docs/v2/07-deployment.md`.
- **Alternative**: Flat layout (`beever_atlas/` at root) — can cause import confusion with editable installs

### 3. ADK tools as stubs in M1
Tool functions in `agents/tools.py` will be defined with correct signatures but raise `NotImplementedError` until stores are implemented in M3/M4. This lets agent scaffolding compile and test without real backends.
- **Alternative**: Skip tools.py until M3 — delays validation of ADK integration patterns

### 4. Health endpoint checks real connections
`GET /api/health` will attempt actual connections to all 4 data stores (Weaviate, Neo4j, MongoDB, Redis) and report per-component status. In Docker Compose, services have health checks so the backend waits for them.
- **Alternative**: Stub health always returning "ok" — defeats the purpose of M1's connectivity proof

### 5. React scaffold with shadcn/ui
Use shadcn/ui (not a component library import — copies components into project) for UI primitives. This gives full control over styling and matches `docs/v2/11-frontend-design.md`.
- **Alternative**: Radix UI directly — more boilerplate, less opinionated defaults
- **Alternative**: Material UI — heavy, opinionated styling doesn't match design spec

### 6. Memories tab uses mock data
The 3-tier memory browser (RES-112) renders with hardcoded mock data in M1. Real API integration happens when Weaviate stores exist (M3).
- **Alternative**: Skip memories tab until M3 — delays frontend validation of the 3-tier UX concept

## Risks / Trade-offs

- **[Docker Compose complexity]** 7 services is a heavy local stack → Mitigation: document minimum RAM (8GB), provide `docker compose up --profile minimal` for backend-only development
- **[ADK version churn]** Google ADK is relatively new → Mitigation: pin version in pyproject.toml, wrap integration points for easy updating
- **[Mock data divergence]** Mock data in memories browser may not match real schemas → Mitigation: define TypeScript types from spec first (`lib/types.ts`), mock data conforms to types
- **[shadcn/ui React 19 compat]** shadcn/ui ecosystem may have React 19 edge cases → Mitigation: use latest shadcn/ui which targets React 19
