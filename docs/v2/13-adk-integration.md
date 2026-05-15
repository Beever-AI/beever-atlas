# ADK & Chat SDK Integration

> **Status**: Implemented (ingestion pipeline, consolidation). Query routing agents are in development.
> **Scope**: Google ADK agent orchestration for ingestion, consolidation, and (planned) Q&A routing

---

## Overview

All LLM-powered operations in Beever Atlas v2 are orchestrated by [Google ADK](https://google.github.io/adk-docs/) agents. This replaces the direct LLM API call pattern with composable agent types (`LlmAgent`, `SequentialAgent`, `ParallelAgent`, `LoopAgent`), each with typed tools and shared session state. Model fallback is handled by [LiteLLM](https://docs.litellm.ai/) — no custom `LLMProvider` class is needed.

The behavioral specs in docs 01-12 (prompts, retrieval logic, quality gates, etc.) remain accurate — they describe *what* each component does. This document describes *how* they are orchestrated.

---

## Google ADK Agent Architecture

### Agent Hierarchy

> **Implementation status**: The ingestion pipeline and consolidation agents are implemented. The query routing agent hierarchy (semantic_agent, graph_agent, response_agent) is the **design spec for the planned Q&A agent** — only a placeholder `echo.py` exists in `agents/query/` today.

#### ✅ Implemented: Ingestion Pipeline

```
ingestion_pipeline (SequentialAgent)  — create_ingestion_pipeline()
│
│   Created by the factory in agents/ingestion/pipeline.py.
│   Processes one NormalizedMessage through 6 stages.
│
├── preprocessor (PreprocessorAgent)
│   Model: none (deterministic, no LLM)
│   Behavior: Stage 1 — Slack mrkdwn → markdown, thread context assembly,
│             bot/system message filtering, media processing (images via Gemini
│             vision, PDFs via pypdf/chunking)
│
├── extraction_parallel (ParallelAgent)
│   │
│   ├── fact_extractor (LlmAgent)        — create_fact_extractor()
│   │   Model: LLM_FAST_MODEL (default: gemini-2.5-flash)
│   │   Behavior: Stage 2 — extract atomic facts, quality gate (reject < 0.5,
│   │             max 2 facts/message)
│   │
│   └── entity_extractor (LlmAgent)      — create_entity_extractor()
│       Model: LLM_FAST_MODEL (default: gemini-2.5-flash)
│       Behavior: Stage 2 — extract entities + relationships, quality gate
│                 (reject confidence < 0.6), filter hypotheticals
│
├── enrich_parallel (ParallelAgent)
│   │
│   ├── embedder (EmbedderAgent)
│   │   Model: none (calls Jina v4 API directly)
│   │   Behavior: Stage 3 — generate 2048-dim named vectors (text + image)
│   │
│   └── cross_batch_validator (LlmAgent) — create_cross_batch_validator()
│       Model: LLM_FAST_MODEL (default: gemini-2.5-flash)
│       Behavior: Stage 3 — resolve entity aliases across message batches,
│                 validate relationship consistency
│
└── persister (PersisterAgent)
    Model: none (rule-based, no LLM)
    Tools: upsert_fact, upsert_entity, create_episodic_link
    Behavior: Stage 4 — outbox persist to Weaviate + Neo4j + MongoDB
              (spec: 05-ingestion-pipeline.md, 08-resilience.md §12.5)
```

#### ✅ Implemented: Consolidation

```
Consolidation is orchestrated by services/consolidation.py (not a LoopAgent).
It uses ADK LlmAgents for summarization:

    create_summarizer() / create_topic_summarizer() / create_channel_summarizer()
        Model: LLM_FAST_MODEL (default: gemini-2.5-flash)
        Behavior: Generate cluster summaries (Tier 1) and channel summaries (Tier 0)
```

#### 🔧 Planned: Q&A Routing Agents

The following agent hierarchy is the **design spec** for the Q&A feature. Only `agents/query/echo.py` (a test placeholder) currently exists.

```
[PLANNED] query_router_agent (Root LlmAgent)
│   Model: LLM_FAST_MODEL
│   Behavior: Query decomposition + understanding → route to semantic/graph/both
│
├── [PLANNED] parallel_retrieval (ParallelAgent)
│   ├── [PLANNED] semantic_agent — 3-tier Weaviate retrieval
│   └── [PLANNED] graph_agent   — Neo4j traversal + Weaviate enrichment
│
└── [PLANNED] response_agent — grounded response + citations
    Model: LLM_QUALITY_MODEL
```

See [`04-query-router.md`](04-query-router.md) for the full design spec of these agents.

### How Agents Use ADK Session State

ADK `Session` objects persist state across the agent hierarchy within a single request:

```python
# query_router_agent writes routing decision to session state
session.state["route"] = "both"
session.state["query_understanding"] = {
    "entities": ["Alice", "JWT"],
    "topics": ["authentication"],
    "semantic_depth": "topic",
    "temporal_scope": "recent",
}

# parallel_retrieval reads state to decide which sub-agents to activate
# semantic_agent writes results to session state
session.state["semantic_results"] = [...]

# graph_agent writes results to session state
session.state["graph_results"] = [...]

# response_agent reads both result sets from session state,
# merges, deduplicates, and generates the final response
```

For the extraction pipeline, session state carries the message through stages:

```python
# preprocessor_agent
session.state["preprocessed"] = {...}

# fact_extractor_agent
session.state["facts"] = [...]
session.state["quality_scores"] = [...]

# entity_extractor_agent
session.state["entities"] = [...]
session.state["relationships"] = [...]

# persister_agent reads all of the above and writes to stores
```

### Key Changes from Original Design

| Component | Original (docs 01-12) | ADK Implementation | Status |
|-----------|----------------------|-------------------|--------|
| Extraction | Pipeline orchestrator calling LLM directly | `SequentialAgent` chaining 6-stage sub-agents | ✅ Implemented |
| Consolidation | Scheduled function calls | `LlmAgent` summarizers via `services/consolidation.py` | ✅ Implemented |
| Query routing | `llm_provider.call("fast", prompt)` | `query_router_agent` with sub-agent delegation | 🔧 Planned |
| Retrieval | Direct function calls to `semantic_retriever` / `graph_retriever` | `ParallelAgent` running `semantic_agent` + `graph_agent` concurrently | 🔧 Planned |
| Response gen | `llm_provider.call("quality", prompt)` | `response_agent` reading from ADK session state | 🔧 Planned |
| LLM model config | Hardcoded model names | `LLMProvider.resolve_model()` reads `LLM_FAST_MODEL` / `LLM_QUALITY_MODEL` env vars | ✅ Implemented |

### ADK Tools

Store operations are wrapped as ADK `FunctionTool` instances. Each tool is a thin wrapper around the corresponding store method — no business logic lives in the tool layer.

| Tool | Wraps | Used By | Spec |
|------|-------|---------|------|
| `search_weaviate_hybrid` | `weaviate_store.search_hybrid()` | semantic_agent, graph_agent | [`02-semantic-memory.md`](02-semantic-memory.md) |
| `get_tier0_summary` | `weaviate_store.get_tier0_summary()` | semantic_agent | [`02-semantic-memory.md`](02-semantic-memory.md) |
| `get_tier1_clusters` | `weaviate_store.get_tier1_clusters()` | semantic_agent, consolidation | [`02-semantic-memory.md`](02-semantic-memory.md) |
| `traverse_neo4j` | `neo4j_store.traverse()` | graph_agent | [`03-graph-memory.md`](03-graph-memory.md) |
| `temporal_chain` | `neo4j_store.temporal_chain()` | graph_agent | [`03-graph-memory.md`](03-graph-memory.md) |
| `comprehensive_traverse` | `neo4j_store.comprehensive_traverse()` | graph_agent | [`03-graph-memory.md`](03-graph-memory.md) |
| `get_episodic_weaviate_ids` | `neo4j_store.get_episodic_weaviate_ids()` | graph_agent | [`03-graph-memory.md`](03-graph-memory.md) |
| `search_tavily` | `external_search.search()` | query_router | [`04-query-router.md`](04-query-router.md) |
| `upsert_fact` | `weaviate_store.upsert_fact()` | persister_agent | [`05-ingestion-pipeline.md`](05-ingestion-pipeline.md) |
| `upsert_entity` | `neo4j_store.upsert_entity()` | persister_agent | [`03-graph-memory.md`](03-graph-memory.md) |
| `create_episodic_link` | `neo4j_store.create_episodic_link()` | persister_agent | [`03-graph-memory.md`](03-graph-memory.md) |

### Model Configuration

Each agent is configured with a model tier. LiteLLM handles transparent fallback when the primary model is unavailable (timeout, rate limit, circuit breaker open). See [`08-resilience.md`](08-resilience.md) for the full fallback chain per agent.

Models are configured via env vars and resolved by `LLMProvider.resolve_model()` in `src/beever_atlas/llm/provider.py`.

| Agent Tier | Env Var | Default Value | Agents |
|-----------|---------|---------------|--------|
| Fast | `LLM_FAST_MODEL` | `gemini-2.5-flash` | fact_extractor, entity_extractor, cross_batch_validator, summarizers |
| Quality | `LLM_QUALITY_MODEL` | `gemini-2.5-flash` | wiki compiler (WikiCompiler) |
| None | — | — | preprocessor, persister, embedder (rule-based / external API) |

### ADK Runner Integration with FastAPI

The FastAPI server creates an ADK `Runner` at startup and uses it to handle all requests:

```python
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

# At startup
session_service = InMemorySessionService()
runner = Runner(
    agent=query_router_agent,
    app_name="beever_atlas",
    session_service=session_service,
)

# Per request (in api_routes.py)
@app.post("/api/search")
async def search(request: SearchRequest):
    session = await session_service.create_session(
        app_name="beever_atlas",
        user_id=request.state.user_id,
    )
    response = await runner.run_async(
        session_id=session.id,
        user_id=request.state.user_id,
        new_message=Content(parts=[Part(text=request.question)]),
    )
    return format_ask_response(response)
```

The same `Runner` serves both MCP tool calls and REST API requests — the agent hierarchy is the single entry point for all LLM-powered operations.

### Observability

ADK agents emit OpenTelemetry spans automatically for each agent invocation, tool call, and model request. These integrate with the existing telemetry pipeline in [`09-observability.md`](09-observability.md) — no additional instrumentation is needed.

---

## Vercel Chat SDK Bot

A TypeScript service (`bot/`) provides real-time chat across Slack, Teams, and Discord.

### Architecture

```
User → Slack/Teams/Discord
         ↓ (real-time events)
    Chat SDK Bot (TypeScript)
         ↓ (REST API calls)
    FastAPI + ADK Runner
         ↓
    ADK Agents → Stores
```

### Event Handlers

| Event | Handler | Action |
|-------|---------|--------|
| `onNewMention` | Subscribe to thread, query backend | Posts Card with answer + citations |
| `onSubscribedMessage` | Process follow-up in thread | Posts answer in thread |
| `onAction("refresh_wiki")` | Call wiki refresh API | Posts confirmation |
| `onAction("sync_channel")` | Call sync API | Posts job ID |

### Platform Adapters

| Platform | Package | State |
|----------|---------|-------|
| Slack | `@chat-adapter/slack` | Redis |
| Teams | `@chat-adapter/teams` | Redis |
| Discord | `@chat-adapter/discord` | Redis |

### Relationship to Python Adapters

The Python `SlackAdapter` (in `src/beever_atlas/adapters/`) handles **batch historical message ingestion** — fetching message history for initial sync.

The Chat SDK bot handles **real-time conversational interaction** — responding to mentions, follow-ups, and action buttons.

Both are needed: batch adapters build the knowledge base, the chat bot surfaces it.

---

## Infrastructure Additions

| Service | Image | Purpose |
|---------|-------|---------|
| `redis` | `redis:7-alpine` | Chat SDK conversation state |
| `bot` | Custom (Node.js) | Chat SDK bot service |

---

## References

- [Google ADK Documentation](https://google.github.io/adk-docs/)
- [Vercel Chat SDK](https://chat-sdk.dev/)
- [Chat SDK Adapters](https://chat-sdk.dev/docs/adapters)
