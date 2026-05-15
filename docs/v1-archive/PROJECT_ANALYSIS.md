# Beever Atlas — Comprehensive Project Analysis

> **Date**: March 20, 2026
> **Version Analyzed**: 3.2.0 (pyproject.toml) / 3.3 (server.py header)
> **Codebase**: 43 Python source files, ~17,700 LOC | 6 test files, ~743 LOC | React frontend (web/)

---

## Table of Contents

1. [Project Overview](#1-project-overview)
2. [Memory Retrieval Architecture Commentary](#2-memory-retrieval-architecture-commentary)
3. [Critical Bugs](#3-critical-bugs)
4. [Limitations](#4-limitations)
5. [Weaknesses](#5-weaknesses)
6. [Incomplete / In-Progress Work](#6-incomplete--in-progress-work)
7. [Further Improvements](#7-further-improvements)
8. [Priority Matrix](#8-priority-matrix)

---

## 1. Project Overview

Beever Atlas is a Slack Context MCP Server that provides AI agents with channel context through hierarchical memory retrieval. It ingests Slack messages, images, PDFs, and videos, processes them through Gemini LLMs for fact extraction, stores them in Weaviate (vector + BM25), and serves them via the MCP protocol with grounded responses and Slack permalink citations.

### Architecture at a Glance

```
Slack API ──► Fetch Phase ──► MongoDB (metadata)
                                  │
                              Process Phase
                                  │
                        Gemini (fact extraction)
                                  │
                           Jina v4 (embeddings)
                                  │
                         Weaviate (vector store)
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
         Tier 0              Tier 1              Tier 2
    Channel Summary     Topic Clusters      Atomic Memories
              │                   │                   │
              └───────────────────┼───────────────────┘
                                  │
                     Hierarchical Retrieval
                                  │
                    ┌─────────────┼─────────────┐
                    ▼             ▼             ▼
               Wiki System   ask_questions   Search Tools
              (FREE reads)   (PAID - LLM)   (hybrid/vector)
                    │             │             │
                    └─────────────┼─────────────┘
                                  ▼
                         MCP Protocol / REST API
                                  │
                            AI Agent Clients
```

### Tech Stack

| Component | Technology |
|-----------|-----------|
| MCP Framework | FastMCP (Python, async) |
| Vector Store | Weaviate (hybrid BM25 + vector, named vectors) |
| LLM (cheap) | Gemini Flash Lite (extraction, tagging, classification) |
| LLM (quality) | Gemini Flash (response generation) |
| Embeddings | Jina v4 (2048-dim, multimodal unified space) |
| Database | MongoDB (sync state, via Motor async driver) |
| Web Search | Tavily |
| Frontend | React + Vite + shadcn/ui ("memory-browser") |
| Deployment | Docker Compose (MCP server, Weaviate, frontend) |

---

## 2. Memory Retrieval Architecture Commentary

### 2.1 Wiki-First Design — Strengths and Gaps

The wiki-first architecture (`wiki://slack/{channel_id}`) is one of the strongest design decisions in the project. It creates a **two-tier cost model**:

1. **FREE tier**: Pre-generated wiki documents cached in MongoDB, served as MCP resources with zero LLM cost per read
2. **PAID tier**: On-demand `ask_questions` with full hierarchical retrieval + Gemini response generation

**What works well:**
- The wiki is generated from the same hierarchical memory system, so it stays consistent with the underlying data
- Topic-level wiki sections (`wiki://slack/{channel}/topics/{topic}`) provide targeted reads without paying for a full LLM call
- The `WikiDocument` model (`models/wiki.py`) has proper structure with sections for overview, topics, decisions, and recent activity
- Wiki generation uses `gemini-2.5-flash-lite` (the cheapest model), keeping regeneration costs low
- The `wiki_cache_ttl_hours` setting (default 24h) prevents unnecessary regeneration

**Gaps and concerns:**

| Issue | Detail | Impact |
|-------|--------|--------|
| **Full regeneration only** | `refresh_wiki` rebuilds the entire wiki document from scratch. There is no incremental update — even a single new message triggers a full regeneration with LLM calls for every section. The `WikiUpdatePlan` and `WikiChangeAnalysis` models exist in `models/wiki.py` but are **never used** in `services/wiki.py`. | Cost waste on large channels |
| **No staleness indicator** | Wiki resources are served without any metadata about when they were last updated. A client reading `wiki://slack/{channel}` has no way to know if the wiki is 1 hour old or 1 week old without calling `list_wikis` separately. | Client trust issues |
| **Wiki generation failures are silent** | If Gemini fails during wiki generation (quota, network, content filter), the error is caught and logged but the wiki is not marked as failed — stale content continues to be served without any indication. | Misleading data |
| **No diff/changelog** | There is no mechanism to show what changed between wiki versions. For teams tracking decisions, knowing "what's new since I last read" is critical. | Reduced utility for recurring readers |
| **Topic limit may drop content** | `wiki_max_topics: int = 20` caps the number of topics in the wiki. Channels with rich discussions across 30+ topics will silently lose coverage. | Information loss |
| **Recent activity is fixed at 7 days** | The `wiki_recent_days: int = 7` is not configurable per-channel and doesn't account for channel activity patterns. A low-traffic channel might have no activity in 7 days; a high-traffic one might need only 2 days. | Poor adaptation |

**Recommendations:**
- Implement the incremental update path using the existing `WikiUpdatePlan` model — detect which sections changed and only regenerate those
- Add `last_updated_at` and `staleness_indicator` to wiki resource metadata
- Consider a diff/changelog section that highlights changes since the last generation
- Make `wiki_recent_days` adaptive based on channel activity (e.g., target N recent memories rather than N days)

### 2.2 Three-Tier Hierarchical Memory — Design Analysis

The 3-tier design maps naturally to how humans think about team knowledge:

```
Tier 0 (Channel Summary)    → "What is this channel about?"
Tier 1 (Topic Clusters)     → "What's happening with authentication?"
Tier 2 (Atomic Memories)    → "Who said we should use JWT, and when?"
```

**What works well:**
- **Query classification** routes queries to the right starting tier automatically, avoiding expensive full-depth searches for simple overview questions
- **Automatic tier expansion** (e.g., if Tier 1 returns < 3 results, expand to Tier 2) provides graceful degradation
- **Temporal decay** (`temporal.py`) applies time-based scoring without re-embedding, keeping recent information prominent
- **Hybrid search** (BM25 + vector) with configurable alpha gives good retrieval for both keyword-precise and semantic queries
- **Cross-modal search** via Jina v4's unified embedding space enables text-to-image and text-to-document retrieval
- **Citation validation** (`grounding.py:165-184`) removes hallucinated citation IDs from LLM responses

**Architectural weaknesses:**

#### 2.2.1 Consolidation Is Fundamentally Broken

This is the most critical issue in the retrieval architecture. The `_link_memories_to_cluster` method at `consolidation.py:214-231` is explicitly a no-op:

```python
async def _link_memories_to_cluster(self, memories, cluster_id):
    # In a future version, we could update memories in Weaviate
    # For now, we track membership in the cluster's member_ids
    logger.debug(f"Linked {len(memories)} memories to cluster {cluster_id}")
```

**The consequence**: Since atomic memories never get their `cluster_id` property set, the `_get_unclustered_memories` method (line 112-136) filters by `not m.get("cluster_id")`, which means **the exact same memories will be re-clustered on every consolidation run**. This creates an ever-growing number of duplicate Tier 1 clusters.

The deterministic UUID generation in `weaviate_client.py:189-223` will prevent exact duplicates, but because the cluster summary is LLM-generated (non-deterministic), even slight text variation produces a new cluster. After N consolidation runs, the system accumulates N near-duplicate clusters per topic, degrading Tier 1 retrieval quality.

**Fix required**: Implement the Weaviate `collection.data.update()` call to set `cluster_id` on atomic memories, or maintain a cluster membership index in MongoDB.

#### 2.2.2 Query Classifier Is Too Brittle

The `QueryClassifier` (`hierarchical_retrieval.py:49-120`) uses hardcoded regex patterns:

```python
TOPIC_PATTERNS = [
    (r"about\s+(?:the\s+)?(\w+)", 1),  # Only captures single word!
    (r"regarding\s+(\w+)", 1),
    ...
]
```

**Problems:**
- **Single-word topic extraction**: "What's happening with API design?" captures only "API", missing the compound topic "API design"
- **Priority ordering creates misclassification**: DETAIL patterns are checked first, so "who said something about authentication" matches `r"who\s+said"` and is classified as DETAIL instead of TOPIC_SPECIFIC, skipping topic extraction entirely
- **No learning or adaptation**: The classifier doesn't use the `model_query_classification` setting that exists in config — there's a Gemini model configured for classification but never called
- **Default fallback is fragile**: Unrecognized queries default to TOPIC_SPECIFIC with `fallback_to_detail: True`, but the `fallback_to_detail` flag is never read by the retrieval logic — it's set but ignored

**Recommendation**: Replace the regex classifier with a lightweight LLM call (using the already-configured `model_query_classification = gemini-2.5-flash-lite`), or at minimum:
- Support multi-word topic extraction via `(\w[\w\s-]+\w)` patterns
- Reorder pattern priority: OVERVIEW → TOPIC → DETAIL (broad to narrow)
- Actually use the `fallback_to_detail` flag in `HierarchicalRetrievalService.retrieve()`

#### 2.2.3 Tier Expansion Logic Has Blind Spots

The tier expansion thresholds are hardcoded magic numbers:

```python
# In retrieve() method
if depth == "summary":
    if len(memories) < 2:  # Why 2?
        # expand to clusters

elif depth == "cluster":
    if len(memories) < 3:  # Why 3?
        # expand to atomic
```

- The thresholds (2 and 3) are arbitrary with no documentation on why these values were chosen
- There is **no expansion from Tier 2 upward** — if an atomic search returns poor results, the system doesn't try broader tiers
- Quality of results is not considered — 3 low-relevance cluster hits won't trigger expansion, even though they might not answer the question
- The expansion adds results via `.extend()` without re-scoring, so expanded results appear at the end regardless of relevance

**Recommendation**:
- Make thresholds configurable
- Consider relevance scores, not just count — expand if max score < threshold
- Re-sort combined results by score after expansion
- Add upward expansion: if Tier 2 returns results but none are high-confidence, check Tier 1 for a cluster summary

#### 2.2.4 Temporal Decay Is Computed But Not Applied to Retrieval

The `TemporalResolutionService` has a well-designed `apply_temporal_decay()` method that adjusts scores by recency. However, examining the call sites:

- `query.py:269` — calls `enrich_memories_with_temporal()` (adds labels only, no score adjustment)
- `grounding.py:82` — calls `enrich_memories_with_temporal()` (same — labels only)
- `hierarchical_retrieval.py` — **never calls temporal service at all** for score adjustment

The `apply_temporal_decay()` method that actually adjusts scores and re-sorts is **never called anywhere in the codebase**. Temporal decay exists as infrastructure but has zero effect on retrieval ranking. Recent and old memories are ranked purely by Weaviate's hybrid search score.

**Impact**: A decision from 6 months ago has the same retrieval weight as one from yesterday, even though the user likely cares more about recent information. The LLM generation prompt mentions temporal preference ("prefer more recent"), but the retrieval layer doesn't enforce it, so the LLM may never see the recent memory if an old one scores higher.

**Fix**: Call `self.temporal_service.apply_temporal_decay(memories)` in `HierarchicalRetrievalService.retrieve()` before returning results.

#### 2.2.5 Deduplication Is Fragile

The deduplication in `hierarchical_retrieval.py:206-213`:

```python
seen_ids = set()
for m in memories:
    mem_id = m.get("id") or m.get("memory", "")[:50]  # First 50 chars as fallback
    if mem_id not in seen_ids:
        seen_ids.add(mem_id)
        unique_memories.append(m)
```

Using the first 50 characters of memory text as a dedup key is unreliable — two memories about the same topic with the same opening ("The team decided to...") will be incorrectly deduplicated. Conversely, two identical facts with different openings will both pass through.

**Recommendation**: Use Weaviate UUIDs exclusively for dedup, and add semantic similarity dedup for cross-tier expansion (e.g., if a cluster summary and its member atomic memories both appear, prefer the more specific one).

#### 2.2.6 No Negative Feedback Loop

The retrieval system has no mechanism to learn from bad retrievals:

- No relevance feedback (user can't mark results as unhelpful)
- No query logs to analyze retrieval quality
- No A/B testing between retrieval strategies
- The grounding service validates citation IDs but doesn't track citation usage rates

For a production system, this means retrieval quality can only be improved by manual tuning of `hybrid_alpha`, thresholds, and patterns — there's no data-driven optimization path.

### 2.3 Retrieval Redesign Proposal — Topic-First with Flexible Tiers

> **Status**: Rough idea / design exploration. Not validated yet.

#### The Core Problem: Tiers Don't Actually Reduce Search Cost

The current 3-tier design gives the illusion of "starting broad and drilling down," but all three tiers query the **same Weaviate collection** via the same `hybrid_search` function. The "tier" is just a metadata property filter — Weaviate still scans the same index regardless. This means:

```
OVERVIEW query  → hybrid_search(tier_filter="tier0_summary", limit=5)   → scans full index
DETAIL query    → hybrid_search(tier_filter="tier2_atomic", limit=20)   → scans full index
```

The retrieval cost is essentially identical. The difference is only in **which pre-computed content** is returned (a stale summary vs. raw facts), not in how efficiently the search runs.

#### Proposed Direction: Topic-First, Then Drill Down

Instead of classifying queries into a tier and searching within that tier, use a **two-stage coarse-to-fine retrieval**:

```
Stage 1: Find relevant topic cluster(s)
  → hybrid_search(tier="tier1_cluster", query=question, limit=5)
  → Identifies which topic areas are relevant
  → Returns cluster member_ids (pointers to atomic memories)

Stage 2: Search WITHIN matched cluster members only
  → hybrid_search(ids=member_ids, query=question, limit=10)
  → Search space narrowed from thousands of memories → tens
  → Much higher precision
```

**Why this is better:**

| Aspect | Current (tier-only) | Topic-first |
|--------|-------------------|-------------|
| Search space for detail queries | All atomic memories in channel | Only memories within matched topic cluster |
| Precision | Diluted — irrelevant topics compete | Focused — pre-filtered by topic relevance |
| Scalability | Degrades linearly as channel grows | Stays bounded — cluster sizes are capped |
| Context quality for LLM | Mixed-topic results | Topically coherent context window |

**Example:**
```
User: "What did the team decide about JWT authentication?"

Current path:
  → Classified as DETAIL → searches ALL tier2_atomic memories
  → Gets 20 results from thousands, hoping "JWT" memories rank high
  → May return noise from unrelated discussions mentioning "team" or "decided"

Topic-first path:
  1. Search clusters → finds "authentication" cluster (15 member memories)
  2. Search within those 15 → "JWT" keywords + semantic match
  → Higher precision, less noise, faster
```

#### Proposed Direction: Bottom-Up Expansion (Low → High)

The current system only expands **top-down** (summary → clusters → atomic). But bottom-up makes sense too:

```
User: "What's the overall project status?"

Current: Classified as OVERVIEW → Tier 0 → returns a stale summary
  (Tier 0 is only updated during consolidation, which may not have run recently)

Bottom-up approach:
  → Start at Tier 2 (always has the freshest data)
  → Get recent atomic memories (last 7 days, limit=30)
  → Group by topic_tags dynamically
  → Synthesize a fresh overview on-the-fly
  → More accurate than a potentially stale Tier 0 summary
```

A **bidirectional** retrieval system would choose direction based on query type:

```
OVERVIEW / "catch me up" questions:
  → Bottom-up: Tier 2 (fresh atomics) → group by topic → synthesize
  → Fresher than pre-computed Tier 0

DETAIL / "who said X" questions:
  → Top-down (topic-first): Tier 1 (find clusters) → Tier 2 (search within)
  → Higher precision than searching all atomics

TOPIC / "what about auth" questions:
  → Direct: Tier 1 cluster + its Tier 2 members
  → Best of both worlds
```

#### Proposed Direction: Score-Based Expansion Instead of Magic Thresholds

Replace the current arbitrary count thresholds (2 and 3) with relevance-score-based decisions:

```python
# Current (arbitrary count thresholds)
if len(memories) < 3:
    expand_to_next_tier()

# Proposed (score-based)
max_score = max(m.get("score", 0) for m in memories) if memories else 0
avg_score = sum(m.get("score", 0) for m in memories) / len(memories) if memories else 0

should_expand = (
    len(memories) == 0              # no results at all
    or max_score < 0.5              # best result is low confidence
    or avg_score < 0.3              # overall quality is poor
)

if should_expand:
    expanded = search_adjacent_tier(direction="down" or "up")
    all_memories = memories + expanded
    # Re-rank combined results by score
    all_memories.sort(key=lambda m: m.get("score", 0), reverse=True)
```

This way expansion happens when results are **low quality**, not just when there are **few results**. Three irrelevant results shouldn't prevent expansion into a tier that might have the answer.

#### Proposed Direction: Flexible / Dynamic Tier Structure

The current system hardcodes exactly 3 tiers. A more flexible approach would treat tiers as a **dynamic hierarchy** rather than a fixed structure:

```
Current (fixed 3 tiers):
  Tier 0: Channel Summary (1 per channel)
  Tier 1: Topic Clusters (N per channel, flat)
  Tier 2: Atomic Memories (many per channel)

Future possibility (dynamic, nested):
  Channel
  ├── Area: "Backend Engineering"
  │   ├── Topic: "Authentication"
  │   │   ├── Sub-topic: "JWT Implementation"
  │   │   │   ├── Fact: "Team chose RS256 over HS256"
  │   │   │   └── Fact: "Token TTL set to 1 hour"
  │   │   └── Sub-topic: "OAuth Integration"
  │   │       └── Fact: "Using Auth0 as provider"
  │   └── Topic: "Database"
  │       └── ...
  └── Area: "Product Design"
      └── ...
```

In this model:
- Tiers are not numbered (0, 1, 2) but represent **semantic depth levels** that emerge from the data
- A channel with 50 messages might only have 2 levels; one with 50,000 might have 4-5
- Consolidation dynamically decides when to create intermediate groupings based on cluster sizes
- Retrieval navigates the tree rather than scanning a flat tier

This is a significant architectural change and needs careful design. Key open questions:
- How to determine when a topic is large enough to warrant sub-topics?
- How to handle memories that belong to multiple topic branches?
- How to keep the tree balanced and prevent degenerate structures?
- Does Weaviate's filtering support efficient tree navigation, or does this need a separate index (e.g., a graph database or MongoDB tree)?

**This is a rough direction, not a concrete proposal.** The immediate priorities are:
1. Fix the consolidation no-op (prerequisite for any cluster-based retrieval)
2. Implement score-based expansion (low effort, high impact)
3. Add topic-first retrieval as an alternative path (medium effort)
4. Evaluate dynamic tier structure as a longer-term evolution

---

### 2.4 Multi-Query Parallel Search — Exists But Disconnected

The project has a well-built multi-query parallel search pipeline in the `agents/` layer:

**What exists:**
- `query_planner.py` — LLM-based query decomposition that splits complex questions into focused sub-queries (internal + external), using `gemini-flash-lite` for cost efficiency
- `parallel_search.py` — True parallel execution via `asyncio.gather()` with deduplication by memory ID and score-based re-ranking
- `coordinator_agent.py` — Wires decomposition → parallel search → grounded response together
- `chat_routes.py` — The web frontend streaming chat uses this full pipeline with thinking tokens

**Example of what it can do:**
```
User: "Tell about NBA and FIFA, are they talking about it?"

query_planner decomposes to:
{
    "internal_queries": [
        {"query": "NBA basketball discussion", "focus": "nba_topic"},
        {"query": "FIFA soccer football", "focus": "fifa_topic"},
        {"query": "NBA FIFA comparison", "focus": "cross_topic"}
    ],
    "external_queries": []
}

search_internal_parallel() runs all 3 via asyncio.gather()
→ Deduplicates by memory ID
→ Sorts by score
→ Returns merged results
```

**The critical gap: The primary MCP tool doesn't use it.**

| Tool | Decomposition | Parallel Search | Used By |
|------|--------------|-----------------|---------|
| `ask_questions()` | None — single query | No | MCP clients (primary tool) |
| `ask_with_context()` | Yes — via coordinator | Yes — asyncio.gather | MCP clients (secondary) |
| `ask_parallel()` | None | No — sequential loop | MCP clients (misleadingly named) |
| Streaming chat API | Yes — with thinking | Yes — asyncio.gather | Web frontend only |

This means:
- An MCP client calling the recommended `ask_questions("Tell about NBA and FIFA")` gets a **single undifferentiated search** that may miss one topic entirely
- The best retrieval path (`ask_with_context`) exists but is positioned as a secondary tool for "external knowledge comparison," not as the default
- The web frontend gets better retrieval quality than MCP clients do

**Recommendation:**
- Make `ask_questions` use the decomposition pipeline by default (or at least when the query classifier detects multiple topics)
- Rename/merge `ask_with_context` and `ask_questions` so the best retrieval path is the default, not an opt-in
- Fix `ask_parallel` to actually use `asyncio.gather()` or remove it (it's misleading)
- Consider making the coordinator path the single entry point for all queries, with the `include_external` flag controlling whether web search is included

---

### 2.5 Grounding and Citation System — Commentary

The grounding system (`grounding.py`) is well-designed for its purpose:

**Strengths:**
- Citation validation removes hallucinated citation IDs via regex (`_validate_citations`)
- Temporal context is injected into the generation prompt so the LLM can reason about recency
- Streaming support with thinking/content separation (`generate_stream`)
- Configurable model override per request

**Weaknesses:**
- `grounding.py:103` — `generate_content()` is a **synchronous blocking call** inside an async function (same issue as Weaviate client)
- `grounding.py:269` — `generate_content_stream()` uses a synchronous `for chunk in ...` loop, which blocks the event loop during streaming
- Citation builder fetches Slack permalinks one at a time (no batching) — for responses with 10 citations, this means 10 sequential Slack API calls
- The `max_citations_per_response` (default 10) limits memories before generation, not citations in the output — if the first 10 memories are all from the same topic, the response loses diversity
- No caching of generated responses — identical questions within minutes trigger full LLM regeneration

### 2.6 Cross-Modal Search — Commentary

The cross-modal search via Jina v4's unified embedding space is a strong differentiator:

**Strengths:**
- Single embedding model for text, images, and documents reduces operational complexity
- Named vectors in Weaviate (`text_vector`, `image_vector`, `doc_vector`) enable targeted cross-modal queries
- Distance threshold filtering (< 0.5) prevents irrelevant cross-modal results from polluting text search

**Weaknesses:**
- The distance threshold (0.5) is hardcoded in `query.py:204` — different embedding spaces may need different thresholds
- Image embeddings are limited to `image_max_tokens: 7500` due to endpoint constraints, which may lose detail for complex diagrams
- No re-ranking step after cross-modal search — text and image results are simply appended, not interleaved by relevance
- PDF page embeddings are per-page, not per-document — a 50-page PDF generates 50 separate embeddings, which fragments retrieval context

---

## 3. Critical Bugs

### 3.0a Internal Error Details Leaked to API Clients

**File**: `api/routes.py` (22 occurrences), `api/chat_routes.py:562`

Every exception handler passes `detail=str(e)` to `HTTPException(status_code=500)`:

```python
except Exception as e:
    raise HTTPException(status_code=500, detail=str(e))  # Leaks internals!
```

This exposes internal stack traces, database connection strings, file paths, and potentially secrets to API clients. Found at lines: 252, 332, 395, 544, 574, 605, 692, 708, 731, 938, 1021, 1092, 1162, 1246, 1331, 1409, 1448, 1476, 1514, 1557.

**Impact**: Information disclosure vulnerability — attackers can learn internal architecture, dependency versions, and potentially credentials from error responses.

**Fix**: Return generic error messages to clients and log full details server-side:
```python
except Exception as e:
    logger.error(f"Failed: {e}", exc_info=True)
    raise HTTPException(status_code=500, detail="Internal server error")
```

### 3.0b Tavily API Key Sent in HTTP Request Body

**File**: `services/external_search.py:145, 263`

The Tavily API key is placed directly in the JSON request body (`"api_key": self.api_key`). If request logging is enabled (common in production), the API key will appear in access logs. The error handler at line 195 includes `str(e)` which could also leak the key.

**Impact**: API key exposure via logs or error messages.

**Fix**: Use the `Authorization` header instead, or ensure request body logging is suppressed for these calls.

### 3.0c `SyncRequest.max_messages` Has No Upper Bound

**File**: `api/schemas.py:121`

`max_messages: int | None = None` has no `Field(le=...)` constraint. A client can pass `max_messages=999999999`, triggering an extremely expensive sync that hits the Slack API thousands of times, incurs massive Gemini API costs, and could cause resource exhaustion. Compare with `SearchRequest.limit` on line 91 which correctly uses `Field(default=20, ge=1, le=100)`.

**Impact**: Denial of service / cost explosion via unbounded sync requests.

**Fix**: Add validation: `max_messages: int = Field(default=500, ge=1, le=5000)`.

### 3.0d `search_scope` Parameter Accepted But Silently Ignored

**File**: `server.py:118-152`

The `ask_questions` tool accepts a `search_scope` parameter (line 138) documented as `"auto"`, `"wiki"`, or `"memories"`, but it is **never passed** to `query_service.ask_question()` on line 150-152. Callers who set `search_scope="wiki"` expecting a different behavior get the same result as the default.

**Impact**: Misleading API contract; callers cannot control retrieval strategy as documented.

**Fix**: Either implement the routing logic for `search_scope` or remove the parameter.

### 3.0e Existing Tests Are Broken

**File**: `tests/test_tools.py:19-20`

`test_tools_are_registered` checks for `"ask_channel"` but the actual tool in `server.py` is `"ask_questions"`. This test will always fail. The expected tools list is outdated and missing many v3.0+ tools (`search_by_topic`, `search_decisions`, `search_recent`, `ask_with_context`, `ask_parallel`, `refresh_wiki`, `list_wikis`, etc.).

**Impact**: The test suite gives false confidence — it doesn't actually validate the current codebase.

**Fix**: Update the expected tools list to match `server.py`.

### 3.1 Consolidation Creates Infinite Duplicate Clusters

**File**: `services/consolidation.py:214-231`

`_link_memories_to_cluster()` is a no-op — atomic memories never get their `cluster_id` set. Every consolidation run re-selects the same memories via `_get_unclustered_memories()` (line 112-136, which filters by `not m.get("cluster_id")`), creating ever-growing duplicate Tier 1 clusters. LLM non-determinism means each run generates slightly different summary text, bypassing the deterministic UUID dedup in Weaviate.

**Impact**: Tier 1 quality degrades over time; duplicate clusters waste storage and confuse retrieval.

**Fix**: Implement `collection.data.update()` in Weaviate to set `cluster_id` on member memories, or track membership in MongoDB.

### 3.2 Synchronous Weaviate Calls Block the Event Loop

**File**: `services/weaviate_client.py:35-63`

The Weaviate client uses the synchronous `weaviate.WeaviateClient`, but all wrapper functions (`hybrid_search`, `insert_multimodal_memory`, `vector_search`) are declared `async`. Every Weaviate call blocks the entire asyncio event loop.

**Impact**: Under concurrent MCP requests, one search blocks all other requests. This is a scalability ceiling.

**Fix**: Wrap all synchronous Weaviate calls in `asyncio.get_event_loop().run_in_executor(None, ...)`, or migrate to the Weaviate async client.

### 3.3 `ask_parallel` Is Sequential, Not Parallel

**File**: `server.py:654-672`

Despite its name, `ask_parallel` uses a sequential `for tier_name in tiers:` loop. The code even acknowledges this with a comment: `# in practice, these could be parallelized`.

**Impact**: Tool name is misleading; multi-tier queries are slower than necessary.

**Fix**: Replace the `for` loop with `asyncio.gather()`.

### 3.4 Naive vs Aware Datetime Comparison Bug

**File**: `services/query.py:89-95`

```python
cutoff = datetime.utcnow() - timedelta(days=max_age_days)  # naive datetime
text_memories = [
    m for m in text_memories
    if self._parse_date(m.get("extracted_at")) >= cutoff  # _parse_date returns aware datetime
]
```

`_parse_date()` (line 411-425) converts "Z" suffixed dates to timezone-aware datetimes (`+00:00`), but `cutoff` is a naive datetime from `datetime.utcnow()`. Comparing aware and naive datetimes **raises TypeError on Python 3.12+**.

There are **89 uses** of the deprecated `datetime.utcnow()` across **16 files**.

**Impact**: Will crash on Python 3.12+ for any time-filtered query.

**Fix**: Replace all `datetime.utcnow()` with `datetime.now(timezone.utc)` across the codebase.

### 3.5 MongoDB Missing from Docker Compose

**File**: `docker-compose.yml`

The compose file defines services for `slack-context-mcp`, `memory-browser`, and `weaviate`, but **MongoDB is not included**. The server requires MongoDB (`mongodb_uri` defaults to `localhost:27017` in `config.py:47`).

**Impact**: `docker-compose up -d` fails with MongoDB connection errors. The documented deployment path is broken.

**Fix**: Add a MongoDB service to `docker-compose.yml`.

### 3.6 `.env` File Leaked Into Docker Image

**File**: `Dockerfile`

```dockerfile
COPY src/ src/
```

This copies `src/slack_context_mcp/.env` (containing API keys for Slack, Google, Jina, Tavily) into the Docker image. There is no `.dockerignore` to exclude it.

**Impact**: Anyone with access to the Docker image can extract all API keys.

**Fix**: Add a `.dockerignore` excluding `**/.env`, or restructure to `COPY` only the needed files.

### 3.7 CORS Hardcoded — Breaks Docker Deployment

**File**: `__main__.py:107-113`

CORS origins are hardcoded to `http://localhost:5173` and `http://127.0.0.1:5173` only. The Docker memory-browser runs on port 3002 with a different origin.

**Impact**: Web frontend cannot communicate with the MCP server in Docker deployments without code changes.

**Fix**: Add `cors_origins: list[str]` to `Settings` in `config.py` and use it in `__main__.py`.

### 3.8 No Graceful Shutdown

**File**: `__main__.py:142-152`

The shutdown event only stops the wiki scheduler. Weaviate client, MongoDB connection, aiohttp sessions (embedding client), and Slack client connections are **never closed**.

**Impact**: Connection leaks during container restarts; potential data corruption if MongoDB writes are in-flight.

**Fix**: Call `close_client()` (Weaviate), `close_database()` (MongoDB), and close aiohttp sessions in the shutdown handler.

---

## 4. Limitations

### 4.1 Single-Workspace Only

The system is designed for one Slack workspace at a time. `slack_bot_token` is a single value, and channel IDs are assumed globally unique. Multi-tenant or multi-workspace deployments require separate instances.

### 4.2 No Authentication/Authorization

- MCP server binds to `0.0.0.0` with no auth (`config.py:52`)
- Weaviate has `AUTHENTICATION_ANONYMOUS_ACCESS_ENABLED: 'true'` (`docker-compose.yml`)
- REST API endpoints have no API keys, JWT, or RBAC
- Anyone on the network can read all Slack content and trigger expensive LLM operations

### 4.3 No Real-Time Sync

Sync is pull-based via manual `sync_channel` calls. There is no Slack event subscription (Socket Mode / Events API). The `slack_app_token` and `slack_signing_secret` config fields exist but are unused — they suggest real-time sync was planned but never implemented.

### 4.4 Embedding Provider Lock-In

Hardcoded to Jina v4 embeddings (2048-dim). Switching providers requires re-embedding all stored memories since vector dimensions are baked into the Weaviate schema's named vectors.

### 4.5 Gemini-Only LLM

All LLM calls go through Google Gemini via `google.genai`. No abstraction layer exists for swapping to OpenAI, Anthropic, or local models. The `google-adk` dependency is tightly coupled throughout agents and services.

### 4.6 Single-Channel Query Scope

All query tools (`ask_questions`, `search_by_topic`, etc.) operate on a single channel. There is no cross-channel search — a user can't ask "what has been discussed about authentication across all channels?"

### 4.7 No Memory Pruning / TTL

Once memories are stored, they persist indefinitely. There is no mechanism to:
- Expire old, low-importance memories
- Archive or compact historical data
- Set per-channel retention policies
- Manage storage growth over time

---

## 5. Weaknesses

### 5.1 Extremely Low Test Coverage

**6 test files / 743 LOC** covering a **17,700 LOC codebase** (~4% test-to-source ratio):

| Service File | Lines | Test Coverage |
|-------------|-------|--------------|
| `services/sync.py` | 1,834 | `test_sync.py` (129 lines) — basic mocks only |
| `api/routes.py` | 1,557 | **None** |
| `services/wiki.py` | 1,154 | **None** |
| `agents/content_analyzer.py` | 1,067 | **None** |
| `server.py` | 881 | `test_tools.py` (53 lines) — parameter registration only |
| `services/weaviate_client.py` | 778 | **None** |
| `agents/batch_analyzer.py` | 666 | **None** |
| `api/chat_routes.py` | 562 | **None** |
| `agents/coordinator_agent.py` | 555 | **None** |
| `services/consolidation.py` | 450 | **None** |
| `services/query.py` | 442 | `test_query.py` (78 lines) — basic mocks |
| `services/hierarchical_retrieval.py` | ~430 | **None** |
| `services/grounding.py` | ~420 | **None** |
| `services/temporal.py` | ~280 | **None** |
| `services/citation.py` | ~250 | **None** |

Only **4 actual test functions** exist in `test_tools.py`, and they only verify parameter registration, not behavior. There are **zero integration tests** and **zero end-to-end tests**.

### 5.2 Broad Exception Swallowing

**16 files** use `except Exception` catches. Many silently catch and log without re-raising:

- `sync.py` — errors during message/file processing are logged but items may be silently dropped
- `wiki.py` — wiki generation failures are caught and logged; stale wiki continues to be served
- `consolidation.py` — cluster creation failures are caught per-topic; remaining topics still run
- `consistency.py` — consistency check errors are swallowed
- `routes.py` — has 5+ bare `pass` statements in exception handlers, swallowing errors completely

### 5.3 God Files

Several files are excessively large and violate single-responsibility:

| File | Lines | Responsibilities |
|------|-------|-----------------|
| `sync.py` | 1,834 | Fetching, processing, batching, retrying, file downloads, user resolution, temp file management |
| `routes.py` | 1,557 | All REST API endpoints for memories, channels, sync, wiki, chat — all in one file |
| `wiki.py` | 1,154 | Wiki generation, caching, section rendering, topic extraction, LLM prompting |
| `content_analyzer.py` | 1,067 | Image analysis, PDF analysis, video analysis, text analysis, temp file handling |

### 5.4 No Connection Health Checks or Reconnection

- Weaviate client (`weaviate_client.py:35-63`) creates a connection once and caches it globally. If the connection drops, all subsequent calls fail with no recovery path.
- MongoDB (`mongodb.py:15-28`) — same pattern, no reconnection.
- Embedding client's aiohttp session (`embeddings.py:244-251`) — no timeout, no retry, no health check.

### 5.5 No Rate Limiting on MCP/API Endpoints

The Slack client handles Slack API rate limits, but the MCP server itself has **no rate limiting**. A consumer can flood `ask_questions` or `sync_channel` with unbounded requests, each triggering expensive Gemini API calls and potentially exhausting quotas.

### 5.6 Embedding Client Has No Retry Logic

The `JinaEmbeddingClient` (`embeddings.py:231-394`) makes single HTTP calls with no retry logic, no timeout configuration, and no rate limiting. Compare this to the Slack client (`slack_client.py:63-127`) which has well-implemented exponential backoff. The embedding client will fail hard on any transient network error.

Additionally, `embed_texts` (line 258-297) sends all texts in a single HTTP request with no batching, which can fail for large lists exceeding the endpoint's payload limit.

### 5.7 Settings Instantiated at Module Import Time

At `config.py:117`, `settings = Settings()` is executed at module import time. Since `slack_bot_token`, `google_api_key`, and `jina_embedding_url` are required fields (no defaults), importing the config module from any test or script fails unless all environment variables are set. This makes unit testing difficult without module-level mocking.

### 5.8 Temporary File Cleanup Risk

`sync.py:1652`, `content_analyzer.py:92-94`, `batch_analyzer.py:257` use `tempfile.NamedTemporaryFile` with `delete=False`. Cleanup relies on manual `os.unlink` in `finally` blocks. If the process crashes between creation and cleanup, temp files accumulate.

---

## 6. Incomplete / In-Progress Work

### 6.1 Dual Agent / Service Architecture

The codebase has **two parallel query execution paths**:

**Services path** (original):
```
QueryService → HierarchicalRetrievalService → GroundedResponseGenerator
```

**Agents path** (newer, ADK-based):
```
CoordinatorAgent → Orchestrator → RetrievalAgents → ParallelSearch
```

The `agents/` directory contains 10 files (coordinator, orchestrator, retrieval agents, query planner, batch analyzer, content analyzer, unified tagging, etc.) that partially duplicate the services layer. `coordinator_agent.py:458` has a `TODO: Collect from agent events`.

It is unclear which path is canonical. The MCP tools in `server.py` use the services path, while `api/chat_routes.py` appears to use the agents path. The ADK migration plan exists in `docs/architecture/11-ADK_MIGRATION_PLAN.md` but is marked as "not started."

### 6.2 Web Frontend (WIP)

The `web/` directory has a React app ("memory-browser") with ~25 components:
- Dashboard, wiki panel, chat interface, sync controls
- Memory visualization, grouped view, detail dialogs
- Channel search/selector, ask-question interface
- AI elements for streaming chat

However:
- No frontend tests (no test runner in `package.json`)
- No E2E tests configured
- Docker builds it but production-readiness is unclear
- `chat_routes.py` (562 lines) provides a streaming chat API separate from MCP

### 6.3 Batch API Toggle

`config.py:63` — `use_batch_api: bool = True` for Gemini Batch API (50% cost savings). However, this was disabled on March 17 due to quota issues. The batch vs. inline processing path adds complexity and the toggle creates two code paths that must both be maintained and tested.

### 6.4 Video Processing

`config.py:70` — `process_videos: bool = True` is configured. Video processing exists in `content_analyzer.py` but is the least mature multimodal path compared to images and PDFs. Frame extraction and analysis quality has not been validated.

### 6.5 Consistency Service (No Automation)

`services/consistency.py` (450 lines) implements MongoDB-to-Weaviate consistency checks (orphan detection, missing entries, stale state recovery). However, there is no automated scheduling — it must be triggered manually. This means inconsistencies can accumulate silently.

### 6.6 No CI/CD Pipeline

There is no `.github/` directory, no GitHub Actions, no GitLab CI, and no automated testing or deployment pipeline. All testing and deployment is manual.

### 6.7 Documentation Out of Sync

- `pyproject.toml` says version `3.2.0`; `server.py` header says `v3.3`
- Architecture docs reference older versions and pre-ADK patterns
- Getting started guide has outdated `.env` patterns

---

## 7. Further Improvements

### 7.1 Security (Critical)

| Improvement | Detail |
|-------------|--------|
| **Add authentication to MCP server and REST API** | API keys at minimum; OAuth2 or JWT for production. Consider MCP protocol-level auth. |
| **Enable Weaviate authentication** | Disable anonymous access; use API keys or OIDC. |
| **Secrets management** | Migrate from `.env` files to a vault (HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager). |
| **Input sanitization** | Channel names and queries flow directly to Weaviate and Gemini without validation. Add input length limits, character filtering, and prompt injection detection. |
| **Docker security hardening** | Run as non-root user, add `.dockerignore`, use multi-stage builds, set `HEALTHCHECK` instruction. |
| **Network isolation** | Weaviate and MongoDB should not be exposed on public ports in production. |

### 7.2 Testing (High Priority)

| Improvement | Detail |
|-------------|--------|
| **Unit tests for every service** | Especially sync, wiki, consolidation, hierarchical_retrieval, query, grounding. Target >70% coverage. |
| **Integration tests with test containers** | Use `testcontainers-python` for Weaviate and MongoDB integration tests. |
| **API/MCP endpoint tests** | Test all MCP tools and REST API endpoints with mocked services. |
| **Frontend tests** | Add Vitest + React Testing Library for component tests; Playwright for E2E. |
| **Load/stress tests** | Validate concurrent request handling given the event loop blocking issues. |
| **Retrieval quality tests** | Create eval datasets to measure retrieval precision/recall across the three tiers. |

### 7.3 Operational Readiness

| Improvement | Detail |
|-------------|--------|
| **CI/CD pipeline** | GitHub Actions with lint, test, build, and Docker image push. |
| **Health checks** | Add readiness/liveness probes that verify Weaviate, MongoDB, and Gemini connectivity. |
| **Metrics/monitoring** | Instrument with OpenTelemetry (already a dependency but unused). Track: query latency, retrieval quality, LLM costs, sync progress, cache hit rates. |
| **Structured logging** | Switch from basic `logging` to JSON structured logs for production aggregation. |
| **Alerting** | Alert on sync failures, LLM quota exhaustion, Weaviate connection drops, consolidation errors. |
| **Graceful shutdown** | Close all connections (Weaviate, MongoDB, aiohttp, Slack) during shutdown. |

### 7.4 Architecture

| Improvement | Detail |
|-------------|--------|
| **Unify agent vs. service paths** | Pick one query execution architecture and remove the other. The ADK agent path is newer but incomplete. |
| **Break up god files** | Split `sync.py` into fetch/process/batch modules. Split `routes.py` by domain (memories, channels, wiki, chat). |
| **Async Weaviate** | Wrap synchronous calls in `run_in_executor()` as immediate fix; migrate to async client long-term. |
| **Connection pooling** | Implement proper connection lifecycle with health checks and reconnection for all external services. |
| **Retry with backoff** | Add exponential backoff for Gemini, Jina, and Tavily API calls (matching the Slack client's existing pattern). |
| **MongoDB transactions** | Use transactions for multi-document updates during sync state changes. |
| **LLM abstraction layer** | Abstract LLM calls behind an interface to enable provider swapping (Gemini, OpenAI, Anthropic, local). |

### 7.5 Features

| Improvement | Detail |
|-------------|--------|
| **Real-time sync** | Implement Slack Socket Mode / Events API for live message ingestion instead of manual pull. |
| **Multi-workspace support** | Support multiple Slack workspaces in a single deployment. |
| **Cross-channel search** | Enable queries across all synced channels. |
| **Incremental wiki updates** | Detect changed sections and only regenerate those (using the existing `WikiUpdatePlan` model). |
| **Memory pruning / TTL** | Implement per-channel retention policies and automatic pruning of old, low-importance memories. |
| **User access control** | Respect Slack channel permissions — private channel memories should only be accessible to authorized users. |
| **Relevance feedback** | Allow users to mark retrieval results as helpful/unhelpful to improve future retrieval quality. |
| **Query result caching** | Cache frequent question/answer pairs with TTL to avoid redundant LLM calls. |
| **Webhook notifications** | Notify when sync completes, wiki updates, or errors occur. |

### 7.6 Cost Optimization

| Improvement | Detail |
|-------------|--------|
| **Embedding caching** | Cache embeddings for repeated or similar queries. |
| **Query result caching** | Cache frequent question answers with TTL. |
| **Message deduplication** | Detect near-duplicate messages before LLM processing. |
| **Adaptive model selection** | Use flash-lite for simple queries and flash/pro only for complex ones (currently all response generation uses flash). |
| **Batch consolidation scheduling** | Run consolidation during off-peak hours to spread API costs. |
| **Token budget tracking** | Track and report LLM token usage per channel and per operation type. |

---

## 8. Priority Matrix

### P0 — Fix Now (Blocking / Data Integrity / Security)

| # | Issue | Type | Effort | Impact |
|---|-------|------|--------|--------|
| 1 | No authentication on MCP server and REST API (24 unprotected endpoints incl. DELETE) | Security | Medium | Unauthorized access to all Slack data + destructive ops |
| 2 | Internal error details leaked via `str(e)` in 22 endpoints | Security | Low | Information disclosure (paths, connection strings, secrets) |
| 3 | Tavily API key sent in HTTP request body (logged in access logs) | Security | Low | API key exposure |
| 4 | `.env` secrets leaked into Docker image via `COPY src/` | Security | Low | API key exposure |
| 5 | Weaviate anonymous access enabled | Security | Low | Unauthorized vector store access |
| 6 | Consolidation no-op creating infinite duplicate clusters | Bug | Low | Data integrity degradation |
| 7 | Synchronous Weaviate calls blocking event loop | Bug | Medium | Scalability ceiling |
| 8 | MongoDB missing from docker-compose | Bug | Low | Deployment broken |
| 9 | `SyncRequest.max_messages` has no upper bound | Security | Low | DoS / cost explosion |
| 10 | Existing tests reference stale tool names — test suite is broken | Bug | Low | False confidence in quality |

### P1 — Fix Soon (Correctness / Reliability)

| # | Issue | Type | Effort | Impact |
|---|-------|------|--------|--------|
| 11 | `datetime.utcnow()` breaks on Python 3.12+ (89 occurrences) | Bug | Medium | Future runtime crashes |
| 12 | CORS hardcoded, breaks Docker deployment | Bug | Low | Frontend broken in Docker |
| 13 | `ask_parallel` is sequential | Bug | Low | Performance / naming mislead |
| 14 | `search_scope` parameter accepted but silently ignored | Bug | Low | Misleading API contract |
| 15 | No graceful shutdown / connection cleanup | Bug | Low | Connection leaks |
| 16 | Temporal decay computed but never applied to retrieval ranking | Design gap | Low | Retrieval quality |
| 17 | No retry logic in embedding client | Reliability | Low | Transient failure crashes |
| 18 | Test coverage at ~4%, zero integration tests | Quality | High | Regression risk |
| 19 | No CI/CD pipeline | Ops | Medium | No automated quality gates |

### P2 — Plan For Next Quarter

| # | Issue | Type | Effort | Impact |
|---|-------|------|--------|--------|
| 20 | Unify agent/service dual architecture | Architecture | High | Maintainability |
| 21 | Break up god files (sync.py, routes.py) | Architecture | Medium | Maintainability |
| 22 | Query classifier too brittle (regex-only, ignores configured LLM) | Design gap | Medium | Retrieval quality |
| 23 | Retrieval redesign: topic-first two-stage search (requires consolidation fix) | Architecture | Medium | Retrieval precision |
| 24 | Retrieval redesign: bottom-up expansion (low→high) for overview queries | Architecture | Medium | Freshness of results |
| 25 | Retrieval redesign: score-based expansion instead of magic thresholds | Architecture | Low | Retrieval quality |
| 26 | Incremental wiki updates (WikiUpdatePlan model exists but unused) | Feature | Medium | Cost savings |
| 27 | Real-time sync via Socket Mode | Feature | High | User experience |
| 28 | Cross-channel search | Feature | Medium | User experience |
| 29 | Memory pruning / TTL | Feature | Medium | Storage management |
| 30 | Settings import-time instantiation (breaks testability) | DX | Low | Testability |
| 31 | Deprecated FastAPI `on_event` usage | Tech debt | Low | Future compatibility |

### P3 — Backlog

| # | Issue | Type | Effort | Impact |
|---|-------|------|--------|--------|
| 32 | Version mismatch (3.2 vs 3.3) | DX | Low | Confusion |
| 33 | Documentation out of sync with v3.3 | DX | Medium | Developer onboarding |
| 34 | LLM abstraction layer (Gemini-only lock-in) | Architecture | High | Provider flexibility |
| 35 | Relevance feedback loop | Feature | High | Retrieval quality |
| 36 | Multi-workspace support | Feature | High | Enterprise readiness |
| 37 | Docker multi-stage build + non-root user | Ops | Low | Image size + security |
| 38 | Structured logging (JSON) | Ops | Medium | Observability |
| 39 | Token budget tracking per channel/operation | Cost | Medium | Cost visibility |
| 40 | Dynamic/flexible tier structure (evolve beyond fixed 3 tiers) | Architecture | High | Long-term scalability |

---

---

## 9. Positive Observations

Despite the issues identified, the project has several well-engineered aspects:

1. **Well-structured configuration management**: The `Settings` class in `config.py` uses pydantic-settings properly, with sensible defaults and clear categorization. Per-task model selection (flash-lite for cheap tasks, flash for quality) is a thoughtful cost optimization.

2. **Deterministic UUID generation for deduplication**: The `_generate_memory_uuid` function in `weaviate_client.py` uses SHA-256 hashing of full content for deterministic deduplication, with a clear comment explaining why the previous approach (first 100 chars) was insufficient.

3. **Adaptive hybrid search**: The `get_adaptive_alpha` function intelligently adjusts BM25-vs-vector balance based on query characteristics (short keyword queries favor BM25; longer semantic queries favor vector). This improves retrieval quality without user tuning.

4. **Graceful degradation throughout**: Many services fail gracefully — returning empty results when Weaviate collections don't exist, falling back to quick extraction when LLM tagging fails, and recovering stuck syncs on startup (`__main__.py` resets stale processing states).

5. **Comprehensive docstrings**: Nearly every public function has thorough docstrings with Args/Returns sections, making the codebase navigable and self-documenting.

6. **Two-phase sync architecture**: The fetch-then-process design (`sync.py`) with MongoDB as an intermediate store ensures no redundant Slack API calls on retry, failed items are automatically retried from local storage, and there's a clear audit trail of fetched vs. processed items.

7. **Citation validation**: The grounding service (`grounding.py:165-184`) actively removes hallucinated citation IDs from LLM responses, preventing the common RAG problem of fabricated references.

8. **Cost-optimized wiki architecture**: The wiki-first approach with free cached reads and paid LLM queries only for specific questions is a well-designed cost optimization that could save significant LLM costs for read-heavy workloads.

---

*Analysis performed by reviewing all 43 source files, 6 test files, Docker/deployment configuration, and documentation. Findings cross-referenced across architecture, code quality, and operational readiness dimensions using 3 parallel analysis agents (architecture, code quality, docs/deployment).*
