# Beever Atlas v2 — Documentation Index

Beever Atlas v2 is a dual-memory knowledge retrieval system for Slack, Teams, and Discord. It combines semantic memory (Weaviate) for fast factual and topic queries with graph memory (Neo4j) for relational and temporal queries, routed by an LLM-powered smart router. The system ingests messages from any supported platform, builds a persistent knowledge base, and surfaces grounded answers with citations.

**Core Frameworks**: [Google ADK](https://google.github.io/adk-docs/) for agent orchestration, [Vercel Chat SDK](https://chat-sdk.dev/) for real-time chat bot, FastAPI for backend, React 19 + shadcn/ui for frontend.

**Key Infrastructure**: Weaviate (semantic), Neo4j + APOC (graph), MongoDB (state), Redis (sessions), Jina v4 (embeddings), Gemini Flash / Claude (LLMs via LiteLLM), Tavily (web search), OpenTelemetry (observability).

---

## Documents

| # | File | Description |
|---|------|-------------|
| 1 | [01-architecture-overview.md](01-architecture-overview.md) | System overview, dual-memory design principle, memory interconnection |
| 2 | [02-semantic-memory.md](02-semantic-memory.md) | Weaviate 3-tier design, schema, retrieval improvements |
| 3 | [03-graph-memory.md](03-graph-memory.md) | Neo4j flexible schema, entity scoping, traversal methods |
| 4 | [04-query-router.md](04-query-router.md) | Smart routing, query decomposition, external search |
| 5 | [05-ingestion-pipeline.md](05-ingestion-pipeline.md) | Multi-platform adapters, 7-stage pipeline, quality gates |
| 6 | [06-wiki-generation.md](06-wiki-generation.md) | Wiki template, consolidation, caching |
| 7 | [07-deployment.md](07-deployment.md) | Docker Compose, MCP tools, module structure |
| 8 | [08-resilience.md](08-resilience.md) | Degradation matrix, circuit breakers, outbox pattern |
| 9 | [09-observability.md](09-observability.md) | Health endpoints, metrics, tracing, backups |
| 10 | [10-access-control.md](10-access-control.md) | Channel ACL, authentication |
| 11 | [11-frontend-design.md](11-frontend-design.md) | Web dashboard UI/UX *(NEW)* |
| 12 | [12-api-design.md](12-api-design.md) | MCP server + REST API interface spec *(NEW)* |
| 13 | [13-adk-integration.md](13-adk-integration.md) | Google ADK agents + Vercel Chat SDK bot *(NEW)* |
| — | [decisions.md](decisions.md) | Key design decisions, open questions, research papers |
| — | [weakness-resolution-map.md](weakness-resolution-map.md) | v1 → v2 weakness fix mapping |

---

## Suggested Reading Order

**For implementation:**

1. `01-architecture-overview.md` — understand the system shape before touching anything else
2. `13-adk-integration.md` — the ADK agent architecture that orchestrates all LLM operations
3. `02-semantic-memory.md` + `03-graph-memory.md` — the two storage backends (can be read in parallel)
4. `05-ingestion-pipeline.md` — how data flows into both backends
5. `04-query-router.md` — how queries are dispatched
6. `06-wiki-generation.md` — the user-facing output layer
7. `07-deployment.md` — running the stack locally
8. `08-resilience.md` + `09-observability.md` — production hardening
9. `10-access-control.md` — multi-tenant security
10. `11-frontend-design.md` + `12-api-design.md` — client-facing surface
11. `decisions.md` + `weakness-resolution-map.md` — context for why things are designed the way they are

---

## v1 Archive

The original monolith proposal and v1 codebase notes are in [`../v1-archive/`](../v1-archive/).
