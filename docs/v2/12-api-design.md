# API Design: MCP Server & REST Interface

## 1. Overview

Beever Atlas exposes two interfaces:

- **REST API** (current): HTTP endpoints consumed by the web dashboard frontend and external integrations. All implemented endpoints are listed in this document.
- **MCP Server** (planned): Tools + Resources for AI assistants (Claude, etc.). The MCP tool specs below describe the planned interface — the underlying service logic exists, but the MCP wrapper layer is not yet the primary interface.

Both interfaces share the same service layer — MCP tools and REST routes call the same underlying functions. There is no separate logic per interface.

> **Status**: REST API is fully implemented. MCP Tools (`ask_questions`, `search_memories`, etc.) are design specs for the planned MCP server layer.

```python
@app.middleware("http")
async def authenticate(request: Request, call_next):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    if not token:
        return JSONResponse(status_code=401, content={"error": "Missing auth token"})
    user = await verify_workspace_token(token)
    request.state.user_id = user.id
    request.state.workspace_id = user.workspace_id
    return await call_next(request)
```

Private channel access is inherited from platform membership. Public channels are visible to all workspace members. Private channel results are filtered via `acl.filter_results()` at the retrieval layer.

---

## 2. MCP Tools

Seven tools are exposed. Graph queries are abstracted behind `ask_questions` — the smart router decides when to use Neo4j. Users and AI clients do not need to know about the dual-memory architecture.

```python
@tool("ask_questions")
async def ask_questions(
    question: str,           # Natural language query
    channel_id: str = None,  # Target channel (None = cross-channel search, ACL-filtered)
    include_citations: bool = True,
    max_results: int = 10,
) -> AskResponse:
    """Ask a question about channel knowledge. Routes automatically
    to semantic search, graph traversal, or both based on query type.
    Cost: $0.001-$0.006 depending on route."""


@tool("search_memories")
async def search_memories(
    query: str,              # Search query
    channel_id: str,
    tier: str = "all",       # "all" | "summary" | "topic" | "atomic"
    limit: int = 15,
    include_images: bool = False,
) -> SearchResponse:
    """Direct hybrid search — bypasses router for power users.
    Cost: ~$0.001"""


@tool("get_wiki")
async def get_wiki(
    channel_id: str,
    section: str = "all",    # "all"|"overview"|"topics"|"people"|"decisions"|"recent"
) -> WikiResponse:
    """Read cached wiki content. FREE for cached sections.
    Returns stale data if wiki is dirty — use refresh_wiki to force update."""


@tool("get_topics")
async def get_topics(
    channel_id: str,
) -> TopicsResponse:
    """List topic clusters for a channel. FREE (cached Tier 1)."""


@tool("sync_channel")
async def sync_channel(
    channel_id: str,
    max_messages: int = 5000,  # Safety limit to prevent cost explosion
    since: str = None,         # ISO timestamp, defaults to last sync point
) -> SyncResponse:
    """Trigger ingestion for a channel. Runs in background.
    Cost: ~$0.0025/message (text), ~$0.008/message (with media)."""


@tool("get_sync_status")
async def get_sync_status(
    channel_id: str = None,    # None = all channels
) -> SyncStatusResponse:
    """Check sync progress and health status. FREE."""


@tool("refresh_wiki")
async def refresh_wiki(
    channel_id: str,
) -> RefreshResponse:
    """Force wiki regeneration. Triggers full reconsolidation.
    Cost: ~$0.01 for LLM synthesis."""
```

---

## 3. MCP Resources

Read-only, URI-based access to pre-rendered wiki content. Resources are served from cache and do not trigger LLM calls.

```python
@resource("wiki://{channel_id}")           # Full wiki markdown
@resource("wiki://{channel_id}/overview")  # Tier 0 summary only
@resource("wiki://{channel_id}/topics")    # Tier 1 cluster list
```

Resources return stale content if the wiki is dirty. Clients should call `refresh_wiki` first if freshness is required.

---

## 4. REST API Endpoints

All endpoints require `Authorization: Bearer <token>`. All responses are `application/json`.

### 4.1 Channels

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/channels` | List all synced channels with metadata |
| GET | `/api/channels/:id` | Get channel details + sync status |
| POST | `/api/channels/:id/sync` | Trigger sync (wraps `sync_channel`) |
| GET | `/api/channels/:id/wiki` | Get wiki content (wraps `get_wiki`) |
| POST | `/api/channels/:id/wiki/refresh` | Force wiki refresh (wraps `refresh_wiki`) |

**GET /api/channels**

Returns all channels the authenticated user can access, ordered by last sync time.

Query params: `platform` (filter by `slack`|`teams`|`discord`), `page`, `limit` (default 50).

**GET /api/channels/:id**

Returns channel metadata, current sync state, and wiki staleness flag.

**POST /api/channels/:id/sync**

Request body:
```json
{
  "max_messages": 5000,
  "since": "2024-01-15T00:00:00Z"
}
```

Returns a `SyncResponse` with `job_id` for polling. Both fields are optional; `since` defaults to the last sync checkpoint.

**GET /api/channels/:id/wiki**

Query params: `section` (`all`|`overview`|`topics`|`people`|`decisions`|`recent`, default `all`).

Returns a `WikiResponse`.

**POST /api/channels/:id/wiki/refresh**

No body. Enqueues a full reconsolidation job. Returns `{ "job_id": "...", "status": "queued" }`.

---

### 4.2 Per-Channel Ask (Streaming)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/channels/:id/ask` | **Streaming Q&A** for a specific channel (SSE) |
| POST | `/api/channels/:id/search/memories` | Direct memory search within channel |

**POST /api/channels/:id/ask** (Server-Sent Events)

The primary query endpoint. Returns an SSE stream showing the agent's thinking, tool calls, and response in real-time.

Request body:
```json
{
  "question": "What did we decide about the auth approach?",
  "include_citations": true,
  "max_results": 10
}
```

Response: `Content-Type: text/event-stream`

```
event: thinking
data: {"content": "Analyzing query... route=graph, entities=[Alice, JWT]..."}

event: tool_call
data: {"tool": "search_weaviate_hybrid", "input_summary": "query='JWT auth'", "output_summary": "5 results, top score 0.87"}

event: tool_call
data: {"tool": "traverse_neo4j", "input_summary": "entities=[Alice]", "output_summary": "Person(Alice) → DECIDED → Decision(RS256)"}

event: response_delta
data: {"content": "Alice decided to use RS256 for JWT"}

event: response_delta
data: {"content": " in the March sprint [1]. This was blocked by"}

event: citations
data: {"citations": [{"id": "c1", "type": "fact", "fact_text": "Alice decided RS256...", ...}, {"id": "c2", "type": "graph", "graph_path": "Person(Alice) → DECIDED → Decision(RS256)", ...}, {"id": "c3", "type": "message", "permalink": "https://slack.com/archives/...", ...}]}

event: metadata
data: {"route_used": "graph", "confidence": 0.92, "cost_usd": 0.005, "degraded": false}

event: done
data: {}
```

**Citation types** (3 kinds per result):
- `fact` — the atomic memory text from Weaviate with quality score
- `graph` — the entity/relationship path from Neo4j
- `message` — permalink to the original Slack/Teams/Discord message

The ADK Runner streams agent events directly to the SSE connection. The `query_router_agent` emits thinking events, each tool call is reported as it happens, and the `response_agent` streams its output token-by-token.

**POST /api/channels/:id/search/memories**

Direct hybrid search within a channel — bypasses the agent for power users who want raw memory results.

Request body:
```json
{
  "query": "authentication JWT refresh tokens",
  "tier": "all",
  "limit": 15,
  "include_images": false
}
```

Returns a `SearchResponse`.

### 4.2.1 Global Cross-Channel Search (Phase 2)

| Method | Path | Description |
|--------|------|-------------|
| POST | `/api/search` | Cross-channel Q&A (searches all joined channels) |

**POST /api/search**

Same request/response shape as per-channel ask, but `channel_id` is derived from the user's joined channels via `acl.get_accessible_channels()`. Results are ACL-filtered.

This is a **Phase 2** feature — the per-channel ask (`/api/channels/:id/ask`) is the priority.

---

### 4.3 Graph

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/graph/entities` | List entities with filters |
| GET | `/api/graph/entities/:id` | Get entity details + relationships |
| GET | `/api/graph/entities/:id/neighbors` | N-hop neighborhood for graph visualization |
| GET | `/api/graph/traverse` | Run traversal from entity names |
| GET | `/api/graph/decisions/:channel_id` | Decision timeline for a channel |

**GET /api/graph/entities**

Query params: `type` (entity type filter), `channel_id`, `q` (name search), `page`, `limit` (default 50).

Returns `{ "entities": EntityResponse[], "total": int, "page": int }`.

**GET /api/graph/entities/:id**

Returns full entity details including all outgoing and incoming relationships.

**GET /api/graph/entities/:id/neighbors**

Query params: `hops` (int, default 1, max 3).

Returns a `GraphNeighborhoodResponse` with nodes and edges suitable for passing directly to a graph visualization library (e.g., D3, Cytoscape).

**GET /api/graph/traverse**

Query params: `from` (entity name), `channel_id` (optional scope), `depth` (default 2).

Runs a Neo4j traversal starting from the named entity. Returns nodes and relationships encountered.

**GET /api/graph/decisions/:channel_id**

Returns a chronological decision timeline for the channel. Supersedes decision chain queries — timeline view is the canonical representation of decision history.

Query params: `since` (ISO timestamp), `limit` (default 100).

---

### 4.4 System

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check across all components |
| GET | `/api/stats` | Aggregate statistics |
| GET | `/api/sync/status` | Global sync status (wraps `get_sync_status`) |

**GET /api/health**

No auth required. Checks MongoDB, Neo4j, vector store, and job queue. Returns `200` if all healthy, `503` if any component is degraded.

**GET /api/stats**

Returns workspace-level aggregate counts: total memories, total entities, total channels synced, approximate storage used.

**GET /api/sync/status**

Query params: `channel_id` (optional; omit for all channels).

Returns a `SyncStatusResponse`.

---

### 4.5 Settings

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/settings` | Get current workspace configuration |
| PUT | `/api/settings` | Update configuration |
| GET | `/api/platforms` | List connected platforms + OAuth status |
| POST | `/api/platforms/:type/connect` | Initiate OAuth flow |

**GET /api/settings**

Returns workspace configuration: daily cost budget, sync defaults, rate limit overrides.

**PUT /api/settings**

Request body (partial update, all fields optional):
```json
{
  "daily_cost_budget_usd": 5.00,
  "default_max_messages": 5000,
  "wiki_auto_refresh": true
}
```

**GET /api/platforms**

Returns connected platforms (`slack`, `teams`, `discord`) with OAuth token status and scopes.

**POST /api/platforms/:type/connect**

`:type` is `slack`, `teams`, or `discord`. Returns an OAuth redirect URL. The frontend should redirect the user to this URL to complete the OAuth handshake.

---

## 5. Response Schemas

### Shared (MCP + REST)

```python
class AskResponse:
    answer: str                    # Grounded response with inline citations
    citations: list[Citation]      # Source facts with platform permalinks
    route_used: str                # "semantic" | "graph" | "both"
    confidence: float              # 0.0-1.0
    degraded: bool                 # True if a component was unavailable
    cost_usd: float                # Estimated cost of this query

class Citation:
    text: str                      # Original fact text
    channel: str                   # Source channel name
    user: str                      # Who said it
    timestamp: str                 # ISO 8601
    permalink: str                 # Platform message URL
    tier: str                      # "atomic" | "topic" | "summary"

class SyncResponse:
    status: str                    # "started" | "already_running" | "queued"
    channel_id: str
    estimated_messages: int        # Approximate message count to process
    job_id: str                    # For tracking via get_sync_status

class WikiResponse:
    content: str                   # Markdown wiki content
    generated_at: str              # ISO 8601
    is_stale: bool                 # True if wiki_dirty flag is set
    channel_id: str
```

### REST-only

```python
class ChannelResponse:
    channel_id: str
    name: str                      # Display name (e.g. "#backend")
    platform: str                  # "slack" | "teams" | "discord"
    is_private: bool
    last_synced_at: str            # ISO 8601, null if never synced
    message_count: int             # Total messages ingested
    wiki_is_stale: bool
    sync_status: str               # "idle" | "running" | "failed"

class EntityResponse:
    id: str                        # Neo4j node ID
    name: str
    type: str                      # Entity type (person, project, decision, etc.)
    channel_id: str                # Source channel (if scoped)
    properties: dict               # Type-specific properties
    relationship_count: int        # Total edges on this node

class GraphNeighborhoodResponse:
    center_id: str
    nodes: list[dict]              # [{id, name, type, properties}]
    edges: list[dict]              # [{source, target, type, properties}]
    hops: int                      # Depth actually returned

class StatsResponse:
    workspace_id: str
    total_memories: int
    total_entities: int
    total_relationships: int
    channels_synced: int
    estimated_storage_mb: float
    last_updated: str              # ISO 8601

class HealthResponse:
    status: str                    # "healthy" | "degraded" | "down"
    components: dict               # {mongodb, neo4j, vector_store, job_queue} -> "up"|"down"
    latency_ms: dict               # Per-component latency
    checked_at: str                # ISO 8601
```

---

## 6. Error Handling

All errors use a consistent envelope:

```json
{
  "error": {
    "code": "CHANNEL_NOT_FOUND",
    "message": "Channel #backend not found or not synced",
    "status": 404
  }
}
```

| Status | Code | Trigger |
|--------|------|---------|
| 400 | `INVALID_QUERY` | Blank or malformed query string |
| 400 | `INVALID_CHANNEL_ID` | Channel ID format invalid |
| 401 | `UNAUTHORIZED` | Missing or unparseable Bearer token |
| 401 | `TOKEN_EXPIRED` | Token is valid but expired |
| 403 | `CHANNEL_ACCESS_DENIED` | Private channel, user is not a member |
| 404 | `CHANNEL_NOT_FOUND` | Channel not synced or does not exist in this workspace |
| 404 | `ENTITY_NOT_FOUND` | Neo4j node ID not found |
| 429 | `RATE_LIMIT_EXCEEDED` | Per-user rate limit or daily cost budget hit |
| 503 | `SERVICE_DEGRADED` | A required component is down; response may be partial |

For `503 SERVICE_DEGRADED`, the response body includes a `degraded_components` array and any partial results that could be returned:

```json
{
  "error": {
    "code": "SERVICE_DEGRADED",
    "message": "Neo4j unavailable — returning semantic results only",
    "status": 503,
    "degraded_components": ["neo4j"]
  },
  "partial_result": { ... }
}
```

---

## 7. Rate Limiting & Cost Controls

### Per-user rate limits

| Endpoint class | Limit |
|----------------|-------|
| Query endpoints (`/api/search/*`, `ask_questions`, `search_memories`) | 60 req/min |
| Sync endpoints (`/api/channels/:id/sync`, `sync_channel`) | 10 req/min |
| Read endpoints (wiki, health, stats, graph) | 120 req/min |

Limits are enforced per `user_id` extracted from the Bearer token. Exceeding a limit returns `429 RATE_LIMIT_EXCEEDED` with a `Retry-After` header (seconds until the window resets).

### Daily cost budget

A configurable `daily_cost_budget_usd` setting (default: $5.00 per workspace) caps total LLM spend. When the budget is exhausted, all cost-incurring operations return `429 RATE_LIMIT_EXCEEDED` with `"code": "DAILY_BUDGET_EXCEEDED"` until the next UTC day.

Cost is tracked per workspace. The `/api/stats` endpoint includes `cost_today_usd` in its response.

### Sync limits

- Default `max_messages` per sync: 5000 (configurable via `/api/settings`)
- Absolute hard cap: 10,000 messages per sync call regardless of setting
- Concurrent syncs per workspace: 3

Sync operations exceeding `max_messages` are truncated at the limit and continue from that point on the next sync call using the checkpoint stored in `last_synced_at`.
