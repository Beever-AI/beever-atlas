# `src/beever_atlas` — Package Map

One-line summary of each top-level package in the Beever Atlas backend.

| Package | Purpose |
|---|---|
| `adapters/` | Chat-platform adapters (Slack, Discord, Teams, Telegram, Mattermost, mock) — translate raw platform webhooks into `NormalizedMessage` objects. |
| `agents/` | ADK `LlmAgent` definitions and prompt templates for extraction, consolidation, media, query, and citation pipelines. |
| `api/` | FastAPI route handlers — one module per resource (channels, connections, ask, wiki, memories, sync, graph, admin, etc.). |
| `capabilities/` | Shared capability layer consumed by both ADK tools and MCP tools — single implementation of memory, sync, wiki, graph, and job operations. |
| `infra/` | Cross-cutting infrastructure: config, auth middleware, crypto, rate limiting, MCP auth/metrics, structured logging, HTTP safety helpers. |
| `llm/` | LLM provider abstraction — Gemini (via ADK), Ollama (local models), and model-tier resolver. |
| `models/` | Pydantic domain models, API request/response schemas, persistence models, platform-connection models, and sync-policy models. |
| `retrieval/` | Vector and hybrid search helpers wrapping Weaviate. |
| `scripts/` | Internal import helpers (`ingest_from_csv`, `import_discord_csv`, dry-run entry point). |
| `server/` | FastAPI `app` factory — mounts all routers, middleware, lifespan, and the optional MCP server. |
| `services/` | Business-logic services: ingestion orchestration, consolidation, contradiction detection, coreference resolution, media/PDF extraction, file import, language detection, wiki scheduling, and reconciliation. |
| `stores/` | Data-store clients — MongoDB, Weaviate, Neo4j, NebulaGraph, null-graph stub, entity registry, chat-history, QA-history, platform store, and file store. |
| `wiki/` | Wiki compiler pipeline — data gathering, LLM compilation, rendering, caching, and builder coordination. |
