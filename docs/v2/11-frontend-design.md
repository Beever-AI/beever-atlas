# Frontend Design: Web Dashboard

> **Status**: Implemented (MVP) — core wiki, ask, memories, graph, and connections pages are live
> **Stack**: React 19 + TypeScript + Vite + TailwindCSS + shadcn/ui
> **Backend**: FastAPI on port 8000, React dev server on port 3000

---

## 1. Overview

The Beever Atlas web dashboard is a **channel-first** knowledge exploration UI. Users see only channels they've joined. Each channel is a self-contained workspace with wiki, Q&A agent, memory browser, and knowledge graph.

**Primary users**: Team leads, engineering managers, anyone browsing channel knowledge.

**Core UX principle**: Click a channel → explore everything about it (wiki, ask questions, browse memories, view graph). Global cross-channel search is secondary.

**Tech stack**:
- React 19 + TypeScript — component framework
- Vite — build tool and dev server
- TailwindCSS + shadcn/ui — styling and component primitives
- TanStack Query (React Query) — server state, caching, polling
- React Router v7 — client-side routing
- cytoscape.js — graph canvas rendering (Graph tab)
- react-markdown + remark-gfm — wiki markdown rendering (tables, lists, strikethrough)
- mermaid — wiki diagram rendering (topic graphs, decision flows, project dependencies)
- recharts — wiki chart rendering (contribution bars, activity trends, topic distribution)
- Custom remark plugins — citation links, entity chips, callout boxes, chart blocks

---

## 2. Pages & Layout

### Site Layout

```
┌─────────────────────────────────────────────────────┐
│ Sidebar (240px)  │  Header (page title + health)    │
│                  │──────────────────────────────────│
│ [Logo]           │                                   │
│                  │  Page Content                     │
│ Dashboard        │                                   │
│ Channels         │                                   │
│  #backend        │                                   │
│  #frontend       │                                   │
│  #design         │                                   │
│ Settings         │                                   │
│                  │                                   │
│ [Health Badge]   │                                   │
└─────────────────────────────────────────────────────┘
```

Sidebar shows **only channels the user has joined** (via `GET /api/channels` which is ACL-filtered). Channels are listed directly in the sidebar nav for quick switching.

---

### 2.1 Dashboard (Home) — `/`

Overview of system health, stats, and **global cross-channel search** (Phase 2 feature).

**Content**:
- Stat cards: total channels synced, total memories, total entities, system health
- Recent activity feed: latest sync completions, new decisions, new entities across all joined channels
- **Global search bar** (cross-channel) — searches across all joined channels. `Cmd+K` shortcut opens it from anywhere. *(Phase 2: initially shows "Coming soon — use per-channel Ask for now")*
- System health badge (polls `GET /api/health` every 30s)

**API calls**: `GET /api/health`, `GET /api/stats`, `GET /api/sync/status`

---

### 2.2 Channel List — `/channels`

Simple list of all joined channels with sync status. Clicking a channel navigates to its workspace.

**Channel list row**: channel name + platform icon, message count, last sync, memory count, sync status badge (idle / syncing / error), "Sync Now" button.

**API calls**: `GET /api/channels`, `GET /api/sync/status`

---

### 2.3 Channel Workspace — `/channels/:id` (THE main page)

This is the core of the application. Each channel is a full workspace with **5 tabs**:

```
┌─────────────────────────────────────────────────────┐
│  #backend-engineering                    [Sync Now]  │
│  ─────────────────────────────────────────────────── │
│  [Wiki] [Ask] [Memories] [Graph] [Settings]          │
│  ═══════                                             │
│                                                       │
│  Tab content area                                     │
│                                                       │
└─────────────────────────────────────────────────────┘
```

#### Tab 1: Wiki — `/channels/:id/wiki` (default tab)

A **pageable, hierarchical knowledge base** for the channel — similar to DeepWiki. Consists of fixed structural pages (always present) and agent-generated topic pages (dynamically created based on channel content). See [`06-wiki-generation.md`](06-wiki-generation.md) for full spec.

**Layout** (two-column: sidebar navigation + page content):
```
┌─────────────────────────────────────────────────────┐
│  #backend-engineering Wiki              [Refresh]    │
│  ─────────────────────────────────────────────────── │
│  ┌──────────┐  ┌──────────────────────────────────┐ │
│  │ PAGES    │  │ Wiki > Topics > Authentication    │ │
│  │          │  │                                    │ │
│  │ 1. Overview│ │ Authentication                    │ │
│  │ 2. Topics │ │ 23 memories · 3 sub-pages          │ │
│  │  2.1 Auth←│ │                                    │ │
│  │    2.1.1  │ │ Team discussed JWT with RS256...   │ │
│  │    2.1.2  │ │                                    │ │
│  │  2.2 Infra│ │ [mermaid: sub-topic graph]         │ │
│  │  2.3 CI/CD│ │                                    │ │
│  │ 3. People │ │ Key Facts                          │ │
│  │ 4. Decide.│ │ • Alice proposed RS256... [1]      │ │
│  │ 5. Tech   │ │ • Migration completed... [2]       │ │
│  │ 6. Project│ │                                    │ │
│  │ 7. Activit│ │ Related Decisions                  │ │
│  │ 8. FAQ    │ │ ✅ Use RS256 — Alice [1]           │ │
│  │ 9. Glossar│ │                                    │ │
│  │ 10.Resourc│ │ Related Media                      │ │
│  │          │  │ 📄 JWT-spec.pdf · 🔗 auth0.com    │ │
│  │          │  │                                    │ │
│  │          │  │ Sub-pages                          │ │
│  │          │  │ → 2.1.1 JWT Migration (12 mem.)    │ │
│  │          │  │ → 2.1.2 OAuth Integration (7 mem.) │ │
│  │          │  │                                    │ │
│  │          │  │ Sources                            │ │
│  │          │  │ [1] @alice · Mar 20 · View ↗      │ │
│  └──────────┘  └──────────────────────────────────┘ │
└─────────────────────────────────────────────────────┘
```

**Pages** (fixed + agent-generated):

| # | Page | Type | Phase | Source |
|---|------|------|-------|--------|
| 1 | **Overview** — summary, stats, topic cards, highlights | Fixed | MVP | Weaviate T0 + MongoDB |
| 2 | **Topic pages** (e.g., "Authentication") — all facts, diagrams, related items | Agent-generated | MVP | Weaviate T1/T2 |
| 2.x | **Sub-pages** (e.g., "JWT Migration") — deep-dive into sub-themes | Agent-generated | 1.5 | Weaviate T2 |
| 3 | **People & Experts** — grouped by role, bar chart | Fixed | MVP | Neo4j |
| 4 | **Decisions** — mermaid flow + timeline + filters | Fixed | MVP | Neo4j |
| 5 | **Tech Stack** — technology grid | Fixed | 1.5 | Neo4j |
| 6 | **Projects** — cards + dependency graph | Fixed | 1.5 | Neo4j |
| 7 | **Recent Activity** — area chart + daily groups | Fixed | MVP | Weaviate T2 |
| 8 | **FAQ** — auto-generated Q&A | Fixed | 1.5 | LLM generation |
| 9 | **Glossary** — channel jargon definitions | Fixed | 2 | LLM extraction |
| 10 | **Resources & Media** — docs, images, links, videos | Fixed | 1.5 | Weaviate T2 media |

**Sidebar navigation**: DeepWiki-style numbered hierarchy. Current page highlighted. Topics section collapsible. Page count badge.

**Cross-cutting**: Inline `[1]` citations on every fact → hover preview + click permalink. Media badges (📄🔗🖼️🎬🎙️) on media-sourced citations. Entity chips (`@alice`, `#topic`, `$tech`) as clickable cross-page navigation. Rich content: Mermaid diagrams, recharts charts, GFM tables, callout boxes.

**Lazy page loading**: `GET /wiki` returns sidebar structure + Overview. Other pages loaded on navigation via `GET /wiki/pages/:page_id`.

**Stale indicator**: Freshness badge on sidebar. Yellow banner + "Refresh Wiki" button when `is_stale === true`.

**API calls**: `GET /api/channels/:id/wiki` (structure + overview), `GET /api/channels/:id/wiki/pages/:page_id` (single page), `GET /api/channels/:id/wiki/structure` (sidebar only), `POST /api/channels/:id/wiki/refresh`

#### Tab 2: Ask — `/channels/:id/ask`

Natural language Q&A agent **with streaming**. This is the primary interaction model.

**Layout**:
```
┌─────────────────────────────────────────────────┐
│  Ask about #backend-engineering                  │
│                                                   │
│  ┌─────────────────────────────────────────────┐ │
│  │ [User question input]              [Submit] │ │
│  └─────────────────────────────────────────────┘ │
│                                                   │
│  ┌─ Agent Response ────────────────────────────┐ │
│  │                                              │ │
│  │  ▸ Thinking... (collapsible CoT)            │ │
│  │    "Analyzing query... route=graph,          │ │
│  │     entities=[Alice, JWT]..."                │ │
│  │                                              │ │
│  │  ▸ Tool: search_weaviate_hybrid             │ │
│  │    → 5 results found                         │ │
│  │                                              │ │
│  │  ▸ Tool: traverse_neo4j                     │ │
│  │    → Person(Alice) → DECIDED → Decision(...) │ │
│  │                                              │ │
│  │  ── Response ──────────────────────────────  │ │
│  │  Alice decided to use RS256 for JWT in the   │ │
│  │  March sprint [1]. This was blocked by...    │ │
│  │                                              │ │
│  │  ── Citations ─────────────────────────────  │ │
│  │  [1] 📝 Fact: "Alice decided RS256..."       │ │
│  │      🔗 Graph: Person(Alice)→Decision(RS256) │ │
│  │      💬 Original: slack.com/archives/...      │ │
│  │                                              │ │
│  │  Route: graph | Confidence: 92% | $0.005     │ │
│  └──────────────────────────────────────────────┘ │
│                                                   │
│  ┌─ Previous Questions ────────────────────────┐ │
│  │  • What auth method did we decide on?        │ │
│  │  • Who is working on the migration?          │ │
│  └──────────────────────────────────────────────┘ │
└───────────────────────────────────────────────────┘
```

**Streaming behavior** (via SSE — Server-Sent Events):
1. User submits question → `POST /api/channels/:id/ask` (streaming)
2. **CoT stream**: Agent's thinking appears in a collapsible section (auto-collapsed after response starts)
3. **Tool call events**: Each tool invocation shows as a step (tool name, brief result summary)
4. **Response stream**: Final answer streams token-by-token with inline citation markers `[1]`, `[2]`
5. **Citations**: After response completes, citation cards render with 3 types:
   - **Fact citation**: The atomic memory text from Weaviate
   - **Graph citation**: The entity/relationship from Neo4j (e.g., `Person(Alice) → DECIDED → Decision(RS256)`)
   - **Original message**: Permalink to the source Slack/Teams/Discord message
6. **Metadata footer**: Route badge (semantic/graph/both), confidence bar, cost

**Per-channel history**: Last 20 questions for this channel stored in localStorage.

**API calls**: `POST /api/channels/:id/ask` (SSE streaming)

#### Tab 3: Memories — `/channels/:id/memories`

Browse the 3-tier memory hierarchy for this channel.

**Layout**: Three-column or accordion view:
- **Tier 0**: Channel summary (single card, always visible at top)
- **Tier 1**: Topic cluster cards. Click to expand → shows member atomic facts.
- **Tier 2**: Searchable list of atomic facts with quality score, timestamp, tags. Filter by topic, entity, importance, date range.

Each memory card shows: text, quality score badge, timestamp, author, topic tags, entity tags. Click → expandable detail with full metadata + link to original message.

**API calls**: `GET /api/channels/:id/wiki?section=overview`, `GET /api/channels/:id/topics`, `POST /api/channels/:id/search/memories`

#### Tab 4: Graph — `/channels/:id/graph`

Channel-scoped knowledge graph visualization.

**Canvas**: cytoscape.js rendering entities as colored nodes:
- Person — blue
- Decision — amber
- Project — green
- Technology — purple
- Team — teal

**Sidebar filters**: entity type checkboxes, time range picker, relationship type filter.

**Interactions**:
- Click node → right panel with entity details + connected entities
- Double-click → expand neighbors (1-hop)
- Hover edge → tooltip with relationship type + timestamp + confidence

**Decision timeline toggle**: Switch from graph view to vertical timeline of Decision nodes showing SUPERSEDES chains.

**API calls**: `GET /api/graph/entities?channel_id=:id`, `GET /api/graph/entities/:eid/neighbors`, `GET /api/graph/decisions/:id`

#### Tab 5: Channel Settings — `/channels/:id/settings`

Per-channel configuration:
- Sync schedule (manual / cron)
- Max messages per sync
- Enabled/disabled toggle
- Last sync details (messages processed, duration, errors)
- "Force Full Re-sync" button

**API calls**: `GET /api/channels/:id`, `POST /api/channels/:id/sync`

---

### 2.4 Settings — `/settings`

**Sections**:
- **Connected Platforms** — Slack / Teams / Discord OAuth cards with connection status
- **System Configuration** — LLM provider, cost limits, embedding model
- **Account** — user profile, workspace info

---

## 3. Component Architecture

```
src/
├── app/
│   ├── layout.tsx              # Root layout: sidebar + header shell
│   ├── page.tsx                # Dashboard home
│   ├── channels/
│   │   ├── page.tsx            # Channel list
│   │   └── [id]/
│   │       ├── layout.tsx      # Channel workspace layout (tab bar)
│   │       ├── wiki/
│   │       │   ├── page.tsx            # Wiki landing → Overview page
│   │       │   ├── layout.tsx          # Wiki layout: sidebar nav + page content
│   │       │   ├── people/page.tsx     # People & Experts page
│   │       │   ├── decisions/page.tsx  # Decisions page
│   │       │   ├── tech-stack/page.tsx # Tech Stack page
│   │       │   ├── projects/page.tsx   # Projects page
│   │       │   ├── activity/page.tsx   # Recent Activity page
│   │       │   ├── faq/page.tsx        # FAQ page
│   │       │   ├── glossary/page.tsx   # Glossary page
│   │       │   ├── resources/page.tsx  # Resources & Media page
│   │       │   └── topics/
│   │       │       ├── [slug]/page.tsx      # Topic page
│   │       │       └── [slug]/[sub]/page.tsx # Sub-topic page
│   │       ├── ask/page.tsx    # Ask agent tab
│   │       ├── memories/page.tsx # 3-tier memory browser
│   │       ├── graph/page.tsx  # Knowledge graph
│   │       └── settings/page.tsx # Channel settings
│   └── settings/
│       └── page.tsx            # Global settings
├── components/
│   ├── layout/
│   │   ├── Sidebar.tsx         # Nav links + channel list, collapse toggle
│   │   ├── Header.tsx          # Page title + global search trigger
│   │   ├── HealthBadge.tsx     # GET /health polling indicator
│   │   └── ChannelTabs.tsx     # Tab bar: Wiki | Ask | Memories | Graph | Settings
│   ├── wiki/
│   │   │  ── Layout ──
│   │   ├── WikiLayout.tsx      # Two-column: sidebar navigation + page content area
│   │   ├── WikiSidebar.tsx     # DeepWiki-style numbered page tree, collapsible, active highlight
│   │   ├── WikiBreadcrumb.tsx  # Breadcrumb: Wiki > Topics > Authentication > JWT Migration
│   │   ├── FreshnessBadge.tsx  # Wiki staleness indicator + refresh button
│   │   │  ── Fixed Pages ──
│   │   ├── OverviewPage.tsx    # Landing: summary, stats, topic cards, highlights, recent changes
│   │   ├── PeoplePage.tsx      # Grouped by role + bar chart + person cards
│   │   ├── DecisionsPage.tsx   # Mermaid supersede flow + vertical timeline + filters
│   │   ├── TechStackPage.tsx   # Technology grid (Phase 1.5)
│   │   ├── ProjectsPage.tsx    # Project cards + mermaid dependency graph (Phase 1.5)
│   │   ├── ActivityPage.tsx    # Area chart + daily grouped activity
│   │   ├── FAQPage.tsx         # Auto-generated Q&A pairs (Phase 1.5)
│   │   ├── GlossaryPage.tsx    # Alphabetical term list (Phase 2)
│   │   ├── ResourcesPage.tsx   # Media grouped by type: docs, images, links, videos (Phase 1.5)
│   │   │  ── Agent-Generated Pages ──
│   │   ├── TopicPage.tsx       # Topic: overview, all facts, related items, sub-page links
│   │   ├── SubTopicPage.tsx    # Sub-topic: summary, all facts, inline media
│   │   │  ── Shared Components ──
│   │   ├── PersonCard.tsx      # Person card: role, expertise chips, decisions
│   │   ├── DecisionEntry.tsx   # Timeline entry: status badge + supersedes + chips
│   │   ├── TopicCard.tsx       # Topic overview card (used on Overview page)
│   │   ├── ProjectCard.tsx     # Project card: lead, status, blockers
│   │   ├── CitationPanel.tsx   # Bottom citations section for any page
│   │   ├── MediaThumbnail.tsx  # Image thumbnail with lightbox + AI alt text
│   │   ├── MediaPreviewCard.tsx # PDF/video/audio preview card with metadata
│   │   ├── MediaBadge.tsx      # Inline media type badge (📄🔗🖼️🎬🎙️) on citations
│   │   │  ── Markdown Rendering ──
│   │   ├── WikiMarkdown.tsx    # Enhanced react-markdown renderer (all plugins)
│   │   ├── MermaidBlock.tsx    # ```mermaid → SVG diagram
│   │   ├── ChartBlock.tsx      # ```chart → recharts (bar, area, donut)
│   │   ├── CalloutBox.tsx      # > [!NOTE] / [!TIP] / [!WARNING] → styled card
│   │   ├── EntityChip.tsx      # @person #topic $tech → clickable navigation chip
│   │   ├── CitationLink.tsx    # [1] → hover preview + click permalink
│   │   └── MediaEmbed.tsx      # Inline image/PDF/video rendering in page content
│   ├── ask/
│   │   ├── AskInput.tsx        # Question input + submit button
│   │   ├── AgentStream.tsx     # Streaming response container
│   │   ├── ThinkingBlock.tsx   # Collapsible CoT thinking section
│   │   ├── ToolCallStep.tsx    # Tool invocation step (name + result summary)
│   │   ├── ResponseBlock.tsx   # Streaming answer with inline citations
│   │   ├── CitationCard.tsx    # Expandable citation: fact + graph + original message
│   │   ├── ResponseMeta.tsx    # Route badge + confidence + cost
│   │   └── QuestionHistory.tsx # Per-channel question history sidebar
│   ├── memories/
│   │   ├── TierBrowser.tsx     # 3-tier accordion/column layout
│   │   ├── SummaryCard.tsx     # Tier 0 channel summary
│   │   ├── ClusterCard.tsx     # Tier 1 topic cluster (expandable)
│   │   ├── FactCard.tsx        # Tier 2 atomic fact with metadata
│   │   └── MemoryFilters.tsx   # Filter by topic, entity, date, importance
│   ├── graph/
│   │   ├── GraphCanvas.tsx     # cytoscape.js wrapper
│   │   ├── EntityPanel.tsx     # Right-side entity detail panel
│   │   ├── GraphFilters.tsx    # Entity type + time + relationship filters
│   │   └── TimelineView.tsx    # Decision timeline toggle view
│   └── channels/
│       ├── ChannelList.tsx     # Sortable table of joined channels
│       ├── ChannelCard.tsx     # Row: name, stats, status badge
│       └── SyncButton.tsx      # Sync Now with optimistic + polling state
├── hooks/
│   ├── useAsk.ts              # SSE streaming for agent Q&A
│   ├── useWiki.ts             # GET /wiki → structure + overview, cache with TanStack Query
│   ├── useWikiPage.ts         # GET /wiki/pages/:id → single page content, cache per page
│   ├── useWikiStructure.ts    # GET /wiki/structure → sidebar tree (lightweight)
│   ├── useWikiRefresh.ts      # POST /wiki/refresh → trigger regeneration + poll until done
│   ├── useMemories.ts         # 3-tier data browsing + search
│   ├── useSync.ts             # sync_channel + get_sync_status polling (5s)
│   ├── useGraph.ts            # entity fetch + neighbor expansion
│   ├── useHealth.ts           # GET /health polling every 30s
│   └── useChannels.ts         # channel list (ACL-filtered)
├── lib/
│   ├── api.ts                 # fetch wrapper: baseURL, error handling, auth header
│   ├── sse.ts                 # Server-Sent Events client for streaming
│   └── types.ts               # TypeScript types mirroring backend schemas
└── styles/
    └── globals.css
```

---

## 4. API Integration

| Page | API Calls |
|------|-----------|
| Dashboard | `GET /api/health`, `GET /api/stats`, `GET /api/sync/status` |
| Channel List | `GET /api/channels` (ACL-filtered to joined channels) |
| Wiki Tab (landing) | `GET /api/channels/:id/wiki` (structure + overview page) |
| Wiki Tab (page nav) | `GET /api/channels/:id/wiki/pages/:page_id` (single page content) |
| Wiki Tab (sidebar) | `GET /api/channels/:id/wiki/structure` (lightweight sidebar tree) |
| Wiki Tab (refresh) | `POST /api/channels/:id/wiki/refresh` (force regeneration) |
| Ask Tab | `POST /api/channels/:id/ask` (SSE streaming) |
| Memories Tab | `GET /api/channels/:id/wiki?section=overview`, `GET /api/channels/:id/topics`, `POST /api/channels/:id/search/memories` |
| Graph Tab | `GET /api/graph/entities?channel_id=`, `GET /api/graph/entities/:id/neighbors`, `GET /api/graph/decisions/:id` |
| Channel Settings | `GET /api/channels/:id`, `POST /api/channels/:id/sync` |
| Global Settings | `GET /api/settings`, `PUT /api/settings`, `GET /api/platforms` |

**Base URL**: `VITE_API_URL` env var, default `http://localhost:8000`.

**Error handling**: non-2xx responses caught by TanStack Query; `ErrorBoundary` components show inline error states.

---

## 5. Key Interaction Flows

### Ask flow (streaming)
1. User types question in `AskInput` on the Ask tab
2. User submits (Enter or button)
3. `useAsk` opens SSE connection to `POST /api/channels/:id/ask`
4. **Event: `thinking`** → `ThinkingBlock` shows collapsible CoT (auto-collapses when response starts)
5. **Event: `tool_call`** → `ToolCallStep` renders tool name + brief result (e.g., "search_weaviate_hybrid → 5 results")
6. **Event: `response_delta`** → `ResponseBlock` streams answer tokens with citation markers `[1]`, `[2]`
7. **Event: `citations`** → `CitationCard[]` render with 3 citation types:
   - Fact citation (Weaviate atomic memory text)
   - Graph citation (Neo4j entity path)
   - Original message (platform permalink)
8. **Event: `done`** → `ResponseMeta` shows route badge, confidence, cost
9. Question appended to per-channel localStorage history

### Sync flow
1. User clicks "Sync Now" in channel settings or channel list
2. `POST /api/channels/:id/sync` → returns `job_id`
3. `useSync` polls every 5s, progress bar updates
4. On completion: toast, wiki stale banner appears on Wiki tab

### Wiki navigation flow
1. User clicks Wiki tab → `GET /api/channels/:id/wiki` returns structure + Overview page
2. Sidebar renders numbered page tree from `structure.pages`
3. User clicks a page in sidebar → `GET /api/channels/:id/wiki/pages/:page_id` loads that page
4. Breadcrumb updates: Wiki > Topics > Authentication > JWT Migration
5. Page content renders via `WikiMarkdown` (mermaid, charts, tables, citations, media)
6. Entity chips (`@alice`, `#topic`) are clickable — navigate to the relevant wiki page

### Wiki refresh flow
1. Yellow stale banner on Wiki sidebar
2. Click "Refresh Wiki" → `POST /api/channels/:id/wiki/refresh`
3. `useWikiRefresh` polls until `is_stale` clears
4. All cached pages invalidated → re-fetch structure + current page with fade transition

### Memory browsing
1. Tier 0 summary card always visible at top
2. Tier 1 topic clusters listed as expandable cards
3. Click cluster → reveals its Tier 2 atomic facts
4. Search/filter across all atomics by topic, entity, date range

### Graph exploration
1. Graph loads with entities from this channel
2. Click node → detail panel on right
3. Double-click → expand neighbors
4. Toggle to timeline view for decision SUPERSEDES chains

---

## 6. Streaming Protocol (SSE Events)

The Ask tab uses Server-Sent Events for real-time agent streaming:

```typescript
// SSE event types from POST /api/channels/:id/ask
interface AskSSEEvents {
  thinking: { content: string };           // CoT reasoning chunk
  tool_call: {
    tool: string;                           // e.g., "search_weaviate_hybrid"
    input_summary: string;                  // e.g., "query='JWT auth', channel=backend"
    output_summary: string;                 // e.g., "5 results, top score 0.87"
  };
  response_delta: { content: string };     // Answer token chunk
  citations: { citations: Citation[] };     // Full citation objects
  metadata: {
    route_used: "semantic" | "graph" | "both";
    confidence: number;
    cost_usd: number;
    degraded: boolean;
  };
  error: { message: string; code: string }; // Error during streaming
  done: {};                                 // Stream complete
}

// Citation types
interface Citation {
  id: string;
  type: "fact" | "graph" | "message";     // 3 citation types
  // Fact citation (from Weaviate)
  fact_text?: string;                      // Atomic memory text
  quality_score?: number;
  tier?: "atomic" | "topic" | "summary";
  // Graph citation (from Neo4j)
  graph_path?: string;                     // e.g., "Person(Alice) → DECIDED → Decision(RS256)"
  entities?: { name: string; type: string }[];
  // Original message citation
  channel: string;
  user: string;
  timestamp: string;
  permalink: string;                       // Platform message URL
}
```

---

## 7. TypeScript Types (Backend Schema Mapping)

```typescript
// lib/types.ts

export interface AskResponse {
  answer: string;
  citations: Citation[];
  route_used: "semantic" | "graph" | "both";
  confidence: number;
  degraded: boolean;
  cost_usd: number;
}

// ── Wiki types (pageable, hierarchical) ──

export interface WikiResponse {
  channel_id: string;
  channel_name: string;
  platform: "slack" | "teams" | "discord";
  generated_at: string;
  is_stale: boolean;
  structure: WikiStructure;      // Sidebar navigation tree
  overview: WikiPage;            // Overview page content (landing)
  metadata: WikiMetadata;
}

export interface WikiStructure {
  channel_id: string;
  channel_name: string;
  platform: string;
  generated_at: string;
  is_stale: boolean;
  pages: WikiPageNode[];         // Top-level page tree
}

export interface WikiPageNode {
  id: string;
  title: string;
  slug: string;
  section_number: string;        // "1", "2.1", "2.1.1"
  page_type: "fixed" | "topic" | "sub-topic";
  memory_count: number;
  children: WikiPageNode[];      // Recursive for sub-pages
}

export interface WikiPage {
  id: string;
  slug: string;
  title: string;
  page_type: "fixed" | "topic" | "sub-topic";
  parent_id: string | null;
  section_number: string;
  content: string;               // Enhanced Markdown (mermaid/chart/callout/media)
  summary: string;               // 1-2 sentence summary for cards/tooltips
  memory_count: number;
  last_updated: string;
  citations: WikiCitation[];
  children: WikiPageRef[];       // Sub-page references
}

export interface WikiPageRef {
  id: string;
  title: string;
  slug: string;
  section_number: string;
  memory_count: number;
}

export interface WikiMetadata {
  member_count: number;
  message_count: number;
  memory_count: number;
  entity_count: number;
  media_count: number;
  page_count: number;            // Total wiki pages
  generation_cost_usd: number;
  generation_duration_ms: number;
}

export interface WikiCitation {
  id: string;                    // "[1]"
  author: string;
  channel: string;
  timestamp: string;
  text_excerpt: string;          // First 100 chars of original message
  permalink: string;             // Slack/Teams/Discord message URL
  media_type?: "pdf" | "image" | "link" | "video" | "audio";
  media_name?: string;           // Filename or domain for media-sourced citations
}

export interface SyncResponse {
  status: "started" | "already_running" | "queued";
  channel_id: string;
  estimated_messages: number;
  job_id: string;
}

export interface SyncStatusResponse {
  channel_id: string;
  state: "idle" | "syncing" | "error";
  progress_pct: number;
  messages_processed: number;
  last_sync_at: string | null;
  error_message: string | null;
}

export interface ChannelResponse {
  channel_id: string;
  name: string;
  platform: "slack" | "teams" | "discord";
  is_private: boolean;
  last_synced_at: string | null;
  message_count: number;
  memory_count: number;
  entity_count: number;
  wiki_is_stale: boolean;
  sync_status: "idle" | "running" | "failed";
}

export interface HealthResponse {
  status: "healthy" | "degraded" | "down";
  components: Record<string, "up" | "down">;
  latency_ms: Record<string, number>;
  checked_at: string;
}

export interface TopicCluster {
  id: string;
  summary: string;
  topic_tags: string[];
  member_count: number;
}

export interface AtomicFact {
  id: string;
  memory: string;
  quality_score: number;
  timestamp: string;
  user_name: string;
  topic_tags: string[];
  entity_tags: string[];
  importance: string;
  permalink: string;
}
```

---

## 8. Design Tokens

**Color scheme**: light theme default, dark mode via `prefers-color-scheme` + manual toggle.

**Palette**:
- Primary: `slate-900` / `slate-50`
- Accent: `indigo-600` — links, active nav, primary buttons
- Success: `emerald-500` — healthy, sync complete
- Warning: `amber-500` — stale wiki, degraded
- Error: `red-500` — sync error, component down
- Graph nodes: blue (Person), amber (Decision), green (Project), purple (Technology), teal (Team)

**Typography**: Inter (UI), JetBrains Mono (code/technical).

**Spacing**: 4px base unit.

**Cards**: `rounded-lg border border-slate-200 shadow-sm hover:shadow-md transition-shadow`

**Sidebar**: 240px expanded, 64px collapsed. State persisted to localStorage.

---

## 9. Backend Routes Required

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/health` | System health check |
| `GET` | `/api/stats` | Aggregate statistics |
| `GET` | `/api/channels` | List joined channels (ACL-filtered) |
| `GET` | `/api/channels/:id` | Channel details + sync status |
| `POST` | `/api/channels/:id/sync` | Trigger sync |
| `GET` | `/api/channels/:id/wiki` | Wiki structure + Overview page (landing) |
| `GET` | `/api/channels/:id/wiki/pages/:page_id` | Single wiki page content (lazy loaded) |
| `GET` | `/api/channels/:id/wiki/structure` | Sidebar navigation tree (lightweight) |
| `POST` | `/api/channels/:id/wiki/refresh` | Force wiki regeneration |
| `GET` | `/api/channels/:id/topics` | Tier 1 topic clusters |
| `POST` | `/api/channels/:id/ask` | **Streaming Q&A** (SSE) — per-channel |
| `POST` | `/api/channels/:id/search/memories` | Search atomic facts in channel |
| `GET` | `/api/graph/entities` | Entity list (channel-scoped) |
| `GET` | `/api/graph/entities/:id/neighbors` | N-hop neighborhood |
| `GET` | `/api/graph/decisions/:channel_id` | Decision timeline |
| `POST` | `/api/search` | **Global cross-channel search** (Phase 2) |
| `GET` | `/api/settings` | Workspace config |
| `PUT` | `/api/settings` | Update workspace config |
| `GET` | `/api/platforms` | Connected platforms |
| `POST` | `/api/platforms/:type/connect` | OAuth flow |

**Key new endpoint**: `POST /api/channels/:id/ask` returns an SSE stream (not JSON). See §6 for the event protocol. This is separate from `POST /api/search` which is the global cross-channel endpoint (Phase 2).
