# Beever Atlas v2: Architecture Overview

> **Status**: Implemented — core pipeline, dual-memory system, wiki generation, and web frontend are operational.
> **Scope**: Production-ready knowledge intelligence system built on dual semantic + graph memory.

---

## 1. Executive Summary

Beever Atlas v1 demonstrated that a wiki-first, hierarchical memory system for Slack channels is viable. However, the demo-stage implementation has 15 validated weaknesses: cluster linking is a no-op, the query classifier uses brittle regex, memory quality is 5.25/10, temporal decay is never applied, and there is no support for relational queries. See [`weakness-resolution-map.md`](weakness-resolution-map.md) for the full mapping of v1 weaknesses to v2 fixes.

**Beever Atlas v2** redesigns the system around two complementary memory systems:

- **Semantic Memory (Weaviate)** — Hierarchical 3-tier memory (improved from v1) handling factual, topic-based, and overview queries via hybrid BM25+vector search. Handles ~80% of queries. Cheap, fast. → [`02-semantic-memory.md`](02-semantic-memory.md)
- **Graph Memory (Neo4j)** — Flexible knowledge graph capturing entity relationships and temporal evolution from conversations. Handles relational queries that semantic search can't answer. ~20% of queries. → [`03-graph-memory.md`](03-graph-memory.md)
- **Smart Router** — LLM-powered query understanding that routes to Semantic, Graph, or both in parallel based on query type and cost optimization. → [`04-query-router.md`](04-query-router.md)

**Design Principle**: Each memory system does what it's best at. They don't duplicate each other's work. Weaviate owns facts and topics. Neo4j owns entities and relationships. The router decides which to use.

---

## 1.1 Technology Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| **Agent Framework** | [Google ADK](https://google.github.io/adk-docs/) (Python) | Orchestrates all LLM-powered operations as composable agents (routing, extraction, response generation). Replaces direct LLM API calls. → [`13-adk-integration.md`](13-adk-integration.md) |
| **Chat Bot** | [Vercel Chat SDK](https://chat-sdk.dev/) (TypeScript) | Real-time conversational interface across Slack, Teams, Discord. Handles mentions, follow-ups, action buttons. → [`13-adk-integration.md`](13-adk-integration.md) |
| **Backend API** | FastAPI (Python) | MCP server + REST API. Shared service layer for both interfaces. → [`12-api-design.md`](12-api-design.md) |
| **Semantic Store** | Weaviate 1.28 | 3-tier hierarchical memory with hybrid BM25+vector search. → [`02-semantic-memory.md`](02-semantic-memory.md) |
| **Graph Store** | Neo4j 5.26 + APOC | Flexible knowledge graph with temporal tracking and multi-hop traversal. → [`03-graph-memory.md`](03-graph-memory.md) |
| **State Store** | MongoDB 7.0 | Sync state, wiki cache, write intents (outbox), quality logs. → [`07-deployment.md`](07-deployment.md) |
| **Session Store** | Redis 7 | Chat SDK conversation state. → [`13-adk-integration.md`](13-adk-integration.md) |
| **Embeddings** | Jina v4 (2048-dim) | Multimodal named vectors (text, image, doc). → [`05-ingestion-pipeline.md`](05-ingestion-pipeline.md) |
| **LLM (fast)** | Gemini 2.0 Flash Lite | Query routing, fact extraction, entity extraction, classification. Fallback: Claude Haiku 4.5 via LiteLLM. → [`08-resilience.md`](08-resilience.md) |
| **LLM (quality)** | Gemini 2.0 Flash | Response generation, wiki synthesis. Fallback: Claude Sonnet 4.6 via LiteLLM. → [`08-resilience.md`](08-resilience.md) |
| **Web Search** | Tavily API | External knowledge grounding (best practices, docs). → [`04-query-router.md`](04-query-router.md) |
| **Frontend** | React 19 + TypeScript + Vite + TailwindCSS + shadcn/ui | Web dashboard for knowledge exploration, graph visualization, admin. → [`11-frontend-design.md`](11-frontend-design.md) |
| **Graph Viz** | cytoscape.js | Interactive knowledge graph canvas in frontend. → [`11-frontend-design.md`](11-frontend-design.md) |
| **Observability** | OpenTelemetry | Distributed tracing, metrics, health checks across all services. → [`09-observability.md`](09-observability.md) |
| **Ingestion** | Python adapters (slack-sdk, MS Graph, discord.py) | Batch historical message fetch from all platforms. → [`05-ingestion-pipeline.md`](05-ingestion-pipeline.md) |

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        BEEVER ATLAS v2 OVERVIEW                        │
│                                                                         │
│                         ┌──────────────┐                                │
│                         │  Smart Query │                                │
│              ┌──────────│    Router    │──────────┐                     │
│              │          └──────────────┘          │                     │
│              ▼                                    ▼                     │
│  ┌─────────────────────────┐     ┌─────────────────────────┐          │
│  │   SEMANTIC MEMORY       │     │    GRAPH MEMORY         │          │
│  │   (Weaviate)            │     │    (Neo4j)              │          │
│  │                         │     │                         │          │
│  │  Tier 0: Summary        │     │  Flexible entities:     │          │
│  │  Tier 1: Topic Clusters │     │  Person, Decision,      │          │
│  │  Tier 2: Atomic Facts   │     │  Project, Technology,   │          │
│  │                         │     │  Team, Meeting, ...     │          │
│  │  Hybrid BM25+Vector     │     │  Flexible relationships │          │
│  │  Cross-modal (img/pdf)  │     │  Temporal tracking      │          │
│  │  Wiki-first (free reads)│     │  Multi-hop traversal    │          │
│  │                         │     │                         │          │
│  │  "What was discussed?"  │     │  "Who decided what?"    │          │
│  │  "Find docs about X"   │     │  "How did X evolve?"    │          │
│  │  "Show me the overview" │     │  "What blocks project?" │          │
│  │                         │     │                         │          │
│  │  ~80% of queries        │     │  ~20% of queries        │          │
│  │  < 200ms, low cost      │     │  200ms-1s, medium cost  │          │
│  └────────────┬────────────┘     └────────────┬────────────┘          │
│               │                                │                       │
│               └────────────┬───────────────────┘                       │
│                            ▼                                           │
│                   ┌──────────────┐                                     │
│                   │   Response   │                                     │
│                   │  Generator   │──▶  Grounded answer + citations     │
│                   └──────────────┘                                     │
│                                                                         │
│  ┌──────────┐    ┌──────────────┐    ┌──────────────┐                  │
│  │  Slack   │    │  Ingestion   │    │   MongoDB    │                  │
│  │  Teams   │───▶│  Pipeline    │    │  (state +    │                  │
│  │  Discord │    │              │───▶│   wiki cache)│                  │
│  └──────────┘    └──────┬───────┘    └──────────────┘                  │
│                         │                                               │
│                    Writes to BOTH                                       │
│                  Weaviate AND Neo4j                                     │
└─────────────────────────────────────────────────────────────────────────┘

> **ADK Agent Layer:** All LLM-powered components above (Query Router, Response
> Generator, Ingestion Pipeline) are implemented as [Google ADK](https://google.github.io/adk-docs/)
> agents. The Query Router is the root `LlmAgent`, retrieval runs via `ParallelAgent`
> (semantic + graph), ingestion via `SequentialAgent`, and consolidation via `LoopAgent`.
> Store operations are wrapped as ADK `FunctionTool` instances. Model fallback is
> handled by LiteLLM. See [`13-adk-integration.md`](13-adk-integration.md) for the
> full agent hierarchy and tool mapping.
```

---

## 2. v1 Weaknesses Summary

Validated against the v1 codebase. Each weakness has a specific fix in v2. See [`weakness-resolution-map.md`](weakness-resolution-map.md) for full detail.

### Critical
| # | Weakness | v2 Fix |
|---|----------|--------|
| 1.11 | Cluster linking is a no-op | Actually write `cluster_id` to atomic memories in Weaviate |
| 1.3 | Detail queries bypass hierarchy | Two-stage topic-first retrieval (Solution A) |
| 1.13 | Memory quality 5.25/10 | Quality gate: reject vague facts, max 2 per message |
| 1.10 | Brittle regex classifier | LLM-powered query understanding (flash-lite) |

### High
| # | Weakness | v2 Fix |
|---|----------|--------|
| 1.4 | Temporal decay never applied | Wire `apply_temporal_decay()` into retrieval ranking |
| 1.1 | Top-down only retrieval | Bidirectional expansion (up + down) |
| 1.2 | Meaningless expansion thresholds | Score-based expansion (`max_score < 0.6`) |
| 1.6 | Slack only | Python adapter layer with NormalizedMessage |

### Medium
| # | Weakness | v2 Fix |
|---|----------|--------|
| 1.5 | No feedback loop | Citation tracking + retrieval quality metrics |
| 1.7 | No real-time sync | Optional Chat SDK webhook bridge (Phase 2) |
| 1.12 | No cross-channel search | Graph memory naturally spans channels |
| 1.14 | No adaptive alpha | Wire `get_adaptive_alpha()` (pass `alpha=None`) |
| 1.15 | No semantic dedup | Jaccard similarity dedup across tiers |

---

## 3. Dual-Memory Architecture

### 3.1 Design Principle: Separation of Concerns

Each memory system handles what it's naturally best at. **They do not duplicate each other.**

| | Semantic Memory (Weaviate) | Graph Memory (Neo4j) |
|---|---|---|
| **What it stores** | Facts, summaries, topic clusters, multimodal content | Entities, relationships, temporal evolution |
| **How it's structured** | 3-tier hierarchy (summary → topics → facts) | Flexible knowledge graph (nodes + edges) |
| **How it's queried** | BM25 + vector hybrid search | Cypher graph traversal |
| **What questions it answers** | "What was discussed about X?", "Show overview", "Find docs" | "Who decided X?", "What blocks Y?", "How did Z evolve?" |
| **Query share** | ~80% (most questions are factual/topical) | ~20% (relational/temporal) |
| **Cost** | Low (embedding search only) | Medium (graph traversal + Weaviate enrichment) |
| **Latency** | < 200ms | 200ms-1s |

**Why not just one?**
- Weaviate can't do multi-hop traversal: "Person → works on → Project → has decision → blocked by → Constraint" requires a graph
- Neo4j can't do fuzzy semantic search across 10K facts with BM25+vector hybrid ranking
- Using both gives us the best of GraphRAG (from reference papers): vector search for finding relevant content + graph traversal for navigating relationships

### 3.2 How the Two Memories Connect

```
┌─────────────────────────────────────────────────────────────────────┐
│                  MEMORY INTERCONNECTION                               │
│                                                                      │
│  INGESTION (writes to BOTH):                                        │
│                                                                      │
│  Message: "Alice decided to use RS256 for JWT — blocked by          │
│            Carol's security review"                                  │
│       │                                                              │
│       ├──▶ WEAVIATE: Atomic fact stored with embedding              │
│       │    memory: "Alice decided to use RS256 for JWT,             │
│       │             blocked by Carol's security review"             │
│       │    id: uuid-abc-123                                          │
│       │    graph_entity_ids: [neo4j-1, neo4j-2, neo4j-3]           │
│       │                                                              │
│       └──▶ NEO4J: Entities + relationships extracted                │
│            Person(Alice) ──DECIDED──▶ Decision(Use RS256)           │
│            Decision(Use RS256) ──USES──▶ Technology(JWT)            │
│            Decision(Use RS256) ──BLOCKED_BY──▶ Person(Carol)        │
│            All entities ──MENTIONED_IN──▶ Event(weaviate_id:        │
│                                                uuid-abc-123)        │
│                                                                      │
│  QUERY (reads from ONE or BOTH):                                    │
│                                                                      │
│  "What was discussed about JWT?"                                    │
│    → Router: SEMANTIC → Weaviate hybrid search → fast, cheap        │
│                                                                      │
│  "Who decided to use RS256?"                                        │
│    → Router: GRAPH → Neo4j traversal:                               │
│      Decision(RS256) ←DECIDED── Person(Alice)                       │
│      → Follow episodic edge → Weaviate(uuid-abc-123) for full text │
│                                                                      │
│  "Tell me about the JWT migration"                                  │
│    → Router: BOTH (ambiguous) → run in parallel:                    │
│      Weaviate: semantic facts about JWT                             │
│      Neo4j: entities related to JWT (people, decisions, blockers)   │
│      → Merge, dedup, rank → comprehensive answer                   │
└─────────────────────────────────────────────────────────────────────┘
```

The cross-reference mechanism:
- Every Weaviate atomic fact stores `graph_entity_ids` — the Neo4j node IDs of entities mentioned in that fact
- Every Neo4j entity node stores a `MENTIONED_IN` edge to an `Event` node, which holds the `weaviate_id` of the source fact
- This bidirectional linking allows graph queries to pull full text from Weaviate, and semantic queries to optionally enrich results with graph context

---

## 4. Individual Component Docs

Each memory system, pipeline stage, and operational concern is documented separately:

### Data Layer
- [`02-semantic-memory.md`](02-semantic-memory.md) — Weaviate 3-tier schema, retrieval improvements, temporal decay, quality boost
- [`03-graph-memory.md`](03-graph-memory.md) — Neo4j flexible schema, entity scoping, traversal methods, episodic linking

### Query & Retrieval
- [`04-query-router.md`](04-query-router.md) — Query decomposition, LLM understanding, cost-optimized routing, external search (Tavily)
- [`06-wiki-generation.md`](06-wiki-generation.md) — Wiki template, cost breakdown, consolidation → cache flow

### Ingestion
- [`05-ingestion-pipeline.md`](05-ingestion-pipeline.md) — 6-stage pipeline, multi-platform adapters, quality gates, entity extraction, contradiction detection, outbox pattern

### Interfaces
- [`12-api-design.md`](12-api-design.md) — MCP tools + REST API spec, response schemas, rate limiting, error handling
- [`11-frontend-design.md`](11-frontend-design.md) — React web dashboard, pages, component architecture, interaction flows
- [`13-adk-integration.md`](13-adk-integration.md) — Google ADK agent hierarchy, Vercel Chat SDK bot, model config, ADK tools

### Operations
- [`07-deployment.md`](07-deployment.md) — Docker Compose, MCP tool spec, module structure
- [`08-resilience.md`](08-resilience.md) — Circuit breakers, degradation matrix, LLM fallback chain, outbox write safety
- [`09-observability.md`](09-observability.md) — Health endpoints, metrics, distributed tracing, backups, cross-store consistency
- [`10-access-control.md`](10-access-control.md) — Channel ACL from platform membership, auth middleware, private channel filtering

### Context & Decisions
- [`decisions.md`](decisions.md) — Key design decisions, open questions, research paper integration
- [`weakness-resolution-map.md`](weakness-resolution-map.md) — v1 → v2 weakness fix mapping (all 15 weaknesses, all 8 solutions)
- [`reference-papers.md`](reference-papers.md) — Detailed analysis of 9 research papers/frameworks informing the design
