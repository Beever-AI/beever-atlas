# Beever Atlas v2 — Architecture Diagram

> Last updated: 2026-03-31 (M3+ implementation — media nodes, entity-facts, graph filtering)

## System Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        BEEVER ATLAS v2 — SYSTEM ARCHITECTURE               │
│              M3+: Ingest & Store (Dual Memory) + Dashboard + Media Graph   │
└─────────────────────────────────────────────────────────────────────────────┘

┌─ FRONTEND (React 19 + Vite + TailwindCSS) ─── web/src/ (54 files) ─────────┐
│                                                                              │
│  Pages                          Hooks                  Components            │
│  ├── Dashboard.tsx              ├── useSync.ts         ├── dashboard/        │
│  │   ├── StatCards              ├── useStats.ts        │   ├── StatCards.tsx  │
│  │   └── ActivityFeed           ├── useGraph.ts        │   └── ActivityFeed  │
│  ├── Channels.tsx               ├── useMemories.ts     ├── channel/          │
│  ├── ChannelWorkspace.tsx       ├── useAsk.ts          │   ├── SyncButton    │
│  │   ├── Wiki tab               ├── useEntityFacts.ts  │   ├── SyncProgress  │
│  │   ├── Ask tab (SSE)          └── useTheme.ts        │   ├── MessagesTab   │
│  │   ├── Messages tab                                  │   └── AskTab        │
│  │   ├── Memories tab ──────── real Weaviate data      ├── memories/         │
│  │   ├── Graph tab ────────── cytoscape.js + sidebar   │   ├── TierBrowser   │
│  │   └── SyncButton + Progress                         │   ├── FactCard      │
│  ├── GraphExplorer.tsx                                 │   ├── ClusterCard   │
│  ├── ActivityPage.tsx                                  │   ├── MemoryFilters │
│  ├── SettingsPage.tsx                                  │   └── SummaryCard   │
│  └── SearchPage.tsx                                    └── graph/            │
│                                                            ├── GraphCanvas   │
│              Polls /api/channels/:id/sync/status           ├── GraphTab      │
│              Entity panel: Details + Facts tabs             ├── EntityPanel   │
│              Media modal: click-to-enlarge images           ├── GraphFilters  │
│                                                            └── MediaModal    │
└──────────────────────────┬───────────────────────────────────────────────────┘
                           │ HTTP (REST + SSE)
┌──────────────────────────▼───────────────────────────────────────────────────┐
│  BACKEND API (FastAPI) ──── src/beever_atlas/api/ ───────────────────────────│
│                                                                              │
│  POST /api/channels/:id/sync ──────── Trigger sync job (auto|full|incr)     │
│  GET  /api/channels/:id/sync/status ─ Poll progress (idle|syncing|error)    │
│  GET  /api/channels/:id/memories ──── Paginated atomic facts (Weaviate)     │
│  GET  /api/channels/:id/memories/:id  Single fact + graph entity enrichment │
│  GET  /api/graph/entities ──────────── List entities (Neo4j, channel filter) │
│  GET  /api/graph/relationships ─────── List relationships (channel filter)  │
│  GET  /api/graph/entities/:id/neighbors  N-hop subgraph (1-5 hops)         │
│  GET  /api/graph/media ─────────────── List media nodes (Neo4j)             │
│  GET  /api/graph/decisions/:channel ── Decision timeline                    │
│  GET  /api/stats ──────────────────── Aggregate counts (all stores)         │
│  GET  /api/activity ───────────────── Recent sync events                    │
│  GET  /api/sync-history ───────────── Sync job history                      │
│  POST /api/channels/:id/ask ───────── SSE streaming Q&A (echo agent)       │
│  DELETE /api/channels/:id/data ─────── Clear synced data (all stores)       │
│  GET  /api/channels/:id/threads/:tid/messages ── Thread replies             │
│  GET  /api/files/proxy ────────────── Proxy Slack file downloads            │
│  GET  /api/health ─────────────────── Component health checks              │
└──────────────────────────┬───────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────────┐
│  SERVICES (Orchestration) ──── src/beever_atlas/services/ ──────────────────│
│                                                                              │
│  SyncRunner                    BatchProcessor           WriteReconciler      │
│  ├── start_sync(channel_id)    ├── process_messages()   ├── run_once()      │
│  ├── _fetch_all_messages()     ├── chunk into batches    ├── retry failed    │
│  │   (cursor pagination        ├── create ADK session   │   Weaviate/Neo4j  │
│  │    >500 msg support)        ├── run pipeline          │   writes          │
│  ├── _run_sync() (background)  └── update progress      └── start_loop()   │
│  └── shutdown() (graceful)                                   (every 15min)  │
│                                                                              │
│  MediaProcessor                                                             │
│  ├── process_media()  ── text-first vision routing                          │
│  └── handles images, PDFs, videos from Slack attachments                    │
└──────────────────────────┬───────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────────┐
│  AGENTS (Google ADK) ──── src/beever_atlas/agents/ ─────────────────────────│
│                                                                              │
│  agents/ingestion/pipeline.py ── create_ingestion_pipeline()                │
│  ┌─────────────────────────────────────────────────────────────┐            │
│  │  SequentialAgent("ingestion_pipeline")                      │            │
│  │  ├── PreprocessorAgent ──── BaseAgent (stage 1, no LLM)    │            │
│  │  │   ├── filters bots/system messages                      │            │
│  │  │   ├── strips Slack mrkdwn                               │            │
│  │  │   └── extracts media URLs + link URLs from attachments  │            │
│  │  ├── ParallelAgent ─────── stages 2+3 run concurrently     │            │
│  │  │   ├── fact_extractor ── LlmAgent (Flash Lite)           │            │
│  │  │   └── entity_extractor  LlmAgent (Flash Lite)           │            │
│  │  ├── classifier ────────── LlmAgent (Flash Lite, stage 4)  │            │
│  │  ├── EmbedderAgent ─────── BaseAgent (Jina API, stage 5)   │            │
│  │  ├── cross_batch_validator  LlmAgent (Flash, stage 6)      │            │
│  │  └── PersisterAgent ────── BaseAgent (outbox, stage 7)     │            │
│  │      ├── writes facts → Weaviate                           │            │
│  │      ├── writes entities/relationships → Neo4j             │            │
│  │      ├── creates Media nodes → Neo4j                       │            │
│  │      ├── reconciles entity↔media via fact references       │            │
│  │      └── creates stub entities for unmatched references    │            │
│  └─────────────────────────────────────────────────────────────┘            │
│                                                                              │
│  agents/prompts/    ── Prompt templates (5 files, independently editable)   │
│  agents/schemas/    ── Pydantic output models (3 files, reusable)          │
│  agents/callbacks/  ── Quality gates (configurable thresholds)             │
│  agents/query/echo.py ── Echo agent (M2, replaced by retrieval in M4)     │
│                                                                              │
│  llm/provider.py ──── LLMProvider (fast/quality tiers, centralized)        │
└──────────────────────────┬───────────────────────────────────────────────────┘
                           │
┌──────────────────────────▼───────────────────────────────────────────────────┐
│  DATA STORES ──── src/beever_atlas/stores/ ─────────────────────────────────│
│                                                                              │
│  ┌─────────────────┐  ┌──────────────────┐  ┌────────────────────────┐     │
│  │  WeaviateStore   │  │  Neo4jStore       │  │  MongoDBStore          │     │
│  │  (Semantic)      │  │  (Graph)          │  │  (State)               │     │
│  │                  │  │                   │  │                        │     │
│  │  MemoryFact      │  │  :Entity nodes    │  │  sync_jobs             │     │
│  │  collection      │  │  :Event nodes     │  │  channel_sync_state    │     │
│  │                  │  │  :Media nodes     │  │  write_intents (outbox)│     │
│  │  Named vectors:  │  │  Relationships    │  │  activity_events       │     │
│  │  text_vector     │  │                   │  │                        │     │
│  │  (2048-dim Jina) │  │  Episodic linking │  │  Reconciler retries    │     │
│  │                  │  │  Entity→Event→    │  │  pending intents       │     │
│  │  Hybrid search   │  │  Weaviate fact    │  │                        │     │
│  │  BM25 + vector   │  │                   │  │  Channel data deletion │     │
│  │                  │  │  REFERENCES_MEDIA │  │  (sync state cleanup)  │     │
│  │  Channel-level   │  │  Entity↔Media     │  │                        │     │
│  │  fact deletion   │  │                   │  │                        │     │
│  │                  │  │  Channel filtering │  │                        │     │
│  │  Media fields:   │  │  via episodic     │  │                        │     │
│  │  source_media_*  │  │  links            │  │                        │     │
│  │  source_link_*   │  │                   │  │                        │     │
│  │                  │  │  APOC fuzzy match │  │                        │     │
│  └────────┬─────────┘  └────────┬──────────┘  └───────────┬────────────┘     │
│           │                     │                          │                 │
│  EntityRegistry (alias resolution, backed by Neo4j)        │                 │
│  StoreClients (singleton, FastAPI lifespan lifecycle)       │                 │
└───────────┼─────────────────────┼──────────────────────────┼─────────────────┘
            │                     │                          │
   ┌────────▼────────┐  ┌────────▼────────┐  ┌──────────────▼──────┐
   │  Weaviate 1.28  │  │  Neo4j 5.26     │  │  MongoDB 7.0       │
   │  :8080          │  │  + APOC         │  │  :27017             │
   │  (Docker)       │  │  :7687 (Docker) │  │  (Docker)           │
   └─────────────────┘  └─────────────────┘  └────────────────────┘
```

## Bot Service

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  BOT SERVICE (TypeScript + Chat SDK) ──── bot/src/ (8 files) ──────────────│
│                                                                              │
│  index.ts ─── Slack bot: @mention → askBackend() → SSE → Slack reply       │
│  bridge.ts ── REST gateway: /bridge/channels, /bridge/messages             │
│               /bridge/files (proxy Slack file downloads)                    │
│               Extracts links, media, unfurls, reactions from Slack API     │
│               Resolves user profiles (parallel, concurrency=8)             │
│               Python backend fetches Slack data through this bridge         │
│  sse-client.ts ── Consumes SSE from Python backend                         │
│  formatter.ts ── Slack Block Kit message formatting                        │
│  slack-mrkdwn.ts ── Slack mrkdwn parsing/stripping                         │
│  *.test.ts ── Unit tests for formatter, sse-client, slack-mrkdwn          │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Data Flow: Channel Sync

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  User clicks "Sync Channel"                                                │
│                                                                              │
│  Dashboard → POST /api/channels/:id/sync                                   │
│    → SyncRunner.start_sync(channel_id)                                     │
│      → Fetch messages via Bridge API (cursor pagination, max 1000)         │
│        → Bridge extracts: text, attachments, links, unfurls, reactions     │
│      → Batch into groups of ~50 (thread-aware grouping)                    │
│      → For each batch:                                                     │
│          → ADK Session (state: messages, channel, known_entities)           │
│          → SequentialAgent runs 7 stages:                                  │
│            1. Preprocess (filter bots, detect modality, extract media/link │
│               URLs from attachments and raw_metadata)                      │
│            2. Extract facts (LLM, quality gate < 0.5)      ┐ parallel     │
│            3. Extract entities (LLM, confidence gate < 0.6) ┘              │
│            4. Classify (topic tags, importance)                            │
│            5. Embed (Jina v4 batch API, 2048-dim)                         │
│            6. Cross-batch validate (alias resolution, consistency)         │
│            7. Persist:                                                     │
│               a. Write facts → Weaviate (with media/link metadata)        │
│               b. Write entities/relationships → Neo4j                     │
│               c. Create Media nodes → Neo4j (with original filenames)     │
│               d. Reconcile entity↔media via fact entity references        │
│               e. Create stub entities for unmatched references            │
│               f. Outbox pattern via MongoDB WriteIntents                  │
│          → Update SyncJob progress in MongoDB                              │
│      → Log activity event                                                  │
│  Dashboard polls status → progress bar updates                             │
│  After sync → Memories tab shows real facts, Graph tab shows entities     │
│             → Media nodes visible in graph, click to enlarge               │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Data Flow: Graph Visualization

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  User opens Graph tab for a channel                                        │
│                                                                              │
│  GraphTab → useGraph(channelId)                                            │
│    → GET /api/graph/entities?channel_id=...                                │
│    → GET /api/graph/relationships?channel_id=...                           │
│    → GET /api/graph/media?channel_id=...                                   │
│    → Neo4j filters entities/relationships via episodic links               │
│    → Media nodes deduplicated by URL                                       │
│                                                                              │
│  GraphCanvas (cytoscape.js)                                                │
│    ├── Renders entity nodes (colored by type)                              │
│    ├── Renders media nodes (distinct color scheme)                         │
│    ├── Renders REFERENCES_MEDIA + entity relationships as edges            │
│    ├── useRef pattern for fresh callbacks (avoids stale closures)          │
│    └── On node click → opens EntityPanel sidebar                           │
│                                                                              │
│  EntityPanel (tabbed sidebar)                                              │
│    ├── Details tab: entity properties, type, aliases                       │
│    └── Facts tab: useEntityFacts(entityName) → Weaviate search            │
│         └── Displays related atomic memories for selected entity           │
│                                                                              │
│  MediaModal                                                                │
│    ├── Triggered by clicking media nodes in graph                          │
│    └── Triggered by clicking image thumbnails in FactCard                  │
│         → Full-size image lightbox with close on backdrop click            │
│                                                                              │
│  GraphFilters                                                              │
│    └── Toggle visibility by entity type (Person, Decision, Project,        │
│        Technology, Media, etc.) with color-coded legend                    │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Backend Directory Structure

```
src/beever_atlas/
├── models/                     # Domain + persistence + API models
│   ├── domain.py              # AtomicFact, GraphEntity, GraphRelationship, Subgraph
│   │                          # Media fields: source_media_urls/names, source_link_urls/titles
│   ├── persistence.py         # SyncJob, ChannelSyncState, WriteIntent, ActivityEvent
│   └── api.py                 # MemoryFilters, PaginatedFacts, HealthResponse
│
├── agents/                     # Agent definitions (WHAT they do)
│   ├── ingestion/             # 7-stage ingestion pipeline
│   │   ├── pipeline.py        # create_ingestion_pipeline() — SequentialAgent wiring
│   │   ├── preprocessor.py    # BaseAgent — stage 1 (no LLM, extracts media/link URLs)
│   │   ├── fact_extractor.py  # Factory → LlmAgent (Flash Lite)
│   │   ├── entity_extractor.py# Factory → LlmAgent (Flash Lite)
│   │   ├── classifier.py      # Factory → LlmAgent (Flash Lite)
│   │   ├── embedder.py        # BaseAgent — stage 5 (Jina API)
│   │   ├── cross_batch_validator.py # Factory → LlmAgent (Flash)
│   │   └── persister.py       # BaseAgent — stage 7 (outbox writes + media nodes
│   │                          #   + entity reconciliation + stub entity creation)
│   ├── query/                 # Retrieval agents (M4 ready)
│   │   └── echo.py            # create_echo_agent() — current root agent
│   ├── prompts/               # Prompt templates (separated from agents)
│   │   ├── fact_extractor.py, entity_extractor.py, classifier.py
│   │   ├── cross_batch_validator.py, echo.py
│   ├── schemas/               # Pydantic output schemas for LLM agents
│   │   ├── extraction.py, classification.py, validation.py
│   ├── callbacks/             # Quality gates & post-processing
│   │   └── quality_gates.py
│   ├── tools.py               # ADK FunctionTool stubs (M4)
│   └── runner.py              # ADK Runner + session helpers
│
├── llm/                        # LLM provider abstraction
│   └── provider.py            # LLMProvider (fast/quality tiers)
│
├── services/                   # Orchestration layer
│   ├── batch_processor.py     # Batch chunking + ADK runner loop
│   ├── sync_runner.py         # Background sync job lifecycle
│   ├── media_processor.py     # Text-first vision routing for multimodal media
│   └── reconciler.py          # Failed write retry (every 15min)
│
├── stores/                     # Data store clients
│   ├── weaviate_store.py      # Semantic memory (3-tier, hybrid search, channel deletion)
│   ├── neo4j_store.py         # Knowledge graph (entities, media nodes, episodic
│   │                          #   channel filtering, REFERENCES_MEDIA edges)
│   ├── mongodb_store.py       # State (sync jobs, outbox, activity, channel cleanup)
│   └── entity_registry.py     # Alias resolution (backed by Neo4j)
│
├── api/                        # REST endpoints
│   ├── ask.py                 # SSE streaming Q&A
│   ├── channels.py            # Channel CRUD + messages + threads + file proxy
│   │                          #   + DELETE channel data (all stores)
│   ├── sync.py                # Sync trigger + progress
│   ├── memories.py            # Atomic facts CRUD
│   ├── graph.py               # Entity/relationship/media listing + subgraph
│   └── stats.py               # Aggregate stats + activity feed + sync history
│
├── adapters/                   # Platform adapters (Slack via bot bridge)
│   ├── base.py                # BaseAdapter, NormalizedMessage, ChannelInfo
│   ├── bridge.py              # ChatBridgeAdapter (calls bot /bridge/*)
│   └── mock.py                # MockAdapter (JSON fixtures)
│
├── infra/                      # Configuration + cross-cutting
│   ├── config.py              # Settings (all env vars, centralized)
│   └── health.py              # Health checks (Weaviate, Neo4j, MongoDB, Redis)
│
└── server/                     # FastAPI app
    └── app.py                 # App creation, lifespan, CORS, routers
```

## File Counts

| Layer | Files | Purpose |
|-------|-------|---------|
| Backend (Python) | 64 | API, agents, stores, services, models |
| Frontend (TS/TSX) | 54 | React pages, hooks, components |
| Bot (TypeScript) | 8 | Slack bot, bridge, SSE client, tests |
| **Total** | **126** | |

## Neo4j Graph Schema

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  NODE TYPES                                                                 │
│                                                                              │
│  :Entity                          :Event                    :Media          │
│  ├── name (indexed)               ├── weaviate_id           ├── url (idx)  │
│  ├── type (indexed)               ├── timestamp             ├── type       │
│  │   (Person, Decision,           ├── channel_id            ├── channel_id │
│  │    Project, Technology)        └── description           └── msg_id     │
│  ├── scope (global|channel)                                                │
│  ├── aliases[]                                                              │
│  └── properties (JSON)                                                      │
│                                                                              │
│  RELATIONSHIP TYPES                                                         │
│                                                                              │
│  (:Entity)-[:DECIDED]->(:Entity)          Decision relationships           │
│  (:Entity)-[:WORKS_ON]->(:Entity)         Assignment / ownership           │
│  (:Entity)-[:USES]->(:Entity)             Technology usage                 │
│  (:Entity)-[:LINKS]->(:Event)             Episodic linking                 │
│  (:Entity)-[:REFERENCES_MEDIA]->(:Media)  Entity↔Media connections         │
│                                                                              │
│  CHANNEL FILTERING                                                          │
│  Entities filtered per-channel via episodic links:                          │
│  Entity→[:LINKS]→Event(channel_id) ensures graph shows only                │
│  entities relevant to the selected channel                                  │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Weaviate Fact Schema

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  Collection: MemoryFact                                                     │
│                                                                              │
│  Core Fields               Source Fields              Tagging               │
│  ├── memory_text           ├── source_message_id      ├── topic_tags[]     │
│  ├── quality_score         ├── message_ts             ├── entity_tags[]    │
│  ├── tier (atomic)         ├── thread_ts              ├── action_tags[]    │
│  ├── importance            ├── author_id              └── graph_entity_ids[]│
│  └── text_vector (2048d)   └── channel_id                                  │
│                                                                              │
│  Media Fields              Link Fields                Temporal              │
│  ├── source_media_urls[]   ├── source_link_urls[]     ├── valid_at         │
│  ├── source_media_names[]  ├── source_link_titles[]   └── invalid_at       │
│  ├── source_media_type     └── source_link_descs[]                         │
│  │   (image/pdf/video)                                                      │
│                                                                              │
│  Search: Hybrid BM25 + vector similarity                                   │
│  Filtering: channel, topic, entity, importance, date range                 │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Milestone Progress

| Milestone | Status | Description |
|-----------|--------|-------------|
| M1: Skeleton & Health Pulse | Done | Bot, Chat SDK, health checks, React scaffold |
| M2: Chat Bot + Echo Query | Done | Echo agent, SSE streaming, bridge API |
| M3: Ingest & Store + Dashboard | Done | 7-stage pipeline, dual stores, full dashboard |
| **M3+: Media & Graph Enhancements** | **Done** | **Media nodes, entity-facts sidebar, channel filtering, image lightbox** |
| M4: Smart Retrieval & Response | Next | Query router, retrieval agents, Ask tab with real answers |
| M5: Consolidation, Wiki & Tiers | Planned | Tier 0/1 generation, wiki builder |
| M6: Contradictions & Retrieval Polish | Planned | Contradiction detection, query decomposition |
| M7: Resilience, Observability & ACL | Planned | Circuit breakers, metrics, access control |
| M8: Multi-Platform & Production | Planned | Teams, Discord, OAuth, production polish |

## Technology Stack

| Layer | Technology |
|-------|-----------|
| Agent Framework | Google ADK (Python) — SequentialAgent, ParallelAgent, LlmAgent |
| LLM (fast) | Gemini 2.0 Flash Lite (extraction, classification) |
| LLM (quality) | Gemini 2.0 Flash (cross-batch validation) |
| Embeddings | Jina v4 (2048-dim, multimodal) |
| Semantic Store | Weaviate 1.28 (hybrid BM25 + vector search) |
| Graph Store | Neo4j 5.26 + APOC (flexible entity schema, media nodes) |
| State Store | MongoDB 7.0 (sync state, outbox pattern) |
| Session Cache | Redis 7 (Chat SDK state) |
| Backend | FastAPI + Pydantic 2 |
| Frontend | React 19 + Vite + TailwindCSS + shadcn/ui |
| Graph Viz | cytoscape.js (with EntityPanel sidebar + MediaModal) |
| Bot | Vercel Chat SDK + @chat-adapter/slack |

## Key Design Patterns

```
┌─────────────────────────────────────────────────────────────────────────────┐
│  DESIGN PATTERNS                                                            │
│                                                                              │
│  Outbox Pattern (Durability)                                                │
│  Write to MongoDB WriteIntent first, then dispatch to Weaviate/Neo4j.      │
│  WriteReconciler retries failed writes every 15 minutes.                   │
│                                                                              │
│  Episodic Channel Filtering                                                 │
│  Entities scoped to channels via Entity→[:LINKS]→Event(channel_id).        │
│  Graph API filters entities, relationships, and counts per channel.        │
│                                                                              │
│  Entity-Media Reconciliation                                                │
│  PersisterAgent creates Media nodes in Neo4j, then links them to entities  │
│  referenced in the same fact. Creates stub entities when no match exists.  │
│                                                                              │
│  useRef Callback Pattern (Frontend)                                         │
│  GraphCanvas uses useRef to keep cytoscape tap handlers fresh without      │
│  destroying the expensive cytoscape instance on re-renders.                │
│                                                                              │
│  Thread-Aware Batching                                                      │
│  BatchProcessor groups messages with their thread replies to avoid          │
│  splitting conversations across batches.                                   │
│                                                                              │
│  Dual Memory Query                                                          │
│  Semantic (Weaviate) for ~80% of queries (factual, topic-based).           │
│  Graph (Neo4j) for ~20% of queries (relational, temporal, decisions).      │
│  Future: Smart router LLM to choose.                                       │
└─────────────────────────────────────────────────────────────────────────────┘
```
