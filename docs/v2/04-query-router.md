# Smart Query Router

> **Status**: Design spec — the query routing logic and agents described here are **in development**. The ingestion pipeline is implemented (see `05-ingestion-pipeline.md`); the Q&A routing layer is next. Only a placeholder agent (`agents/query/echo.py`) currently exists.

Queries arrive from the API layer and pass through three steps before any retrieval happens: decomposition into sub-queries, LLM-powered understanding of each sub-query, and routing to one or both memory stores (or external search). Results from all branches are merged into a single ranked response.

Underlying stores: see [`02-semantic-memory.md`](./02-semantic-memory.md) (Weaviate) and [`03-graph-memory.md`](./03-graph-memory.md) (Neo4j).

> **ADK Implementation:** The entire query flow is orchestrated by the `query_router_agent` (an ADK `LlmAgent`), which delegates to a `retrieval_pipeline` (`ParallelAgent` running `semantic_agent` + `graph_agent`) and a `response_agent`. The behavioral specs below describe *what* each step does; the ADK agent hierarchy in [`13-adk-integration.md`](13-adk-integration.md) describes *how* they are orchestrated.

---

## 4.0 Query Decomposition

Complex questions are decomposed into focused parallel sub-queries before routing. This was a key v1 feature that the v2 router must preserve and enhance.

```python
class QueryDecomposer:
    """Decompose complex questions into parallel sub-queries.

    Example:
    "What auth method did we decide on and how does it compare to best practices?"
    → internal_queries:
        - {"query": "authentication decision JWT", "focus": "decision"}
        - {"query": "OAuth implementation alice", "focus": "implementation"}
    → external_queries:
        - {"query": "JWT vs OAuth best practices 2025", "focus": "comparison"}
    """

    async def decompose(self, question: str) -> QueryPlan:
        """Break down a question into internal + external sub-queries."""
        # Fast path: simple questions → single internal query, no decomposition
        if self._is_simple(question):
            return QueryPlan(
                internal_queries=[{"query": question, "focus": "direct"}],
                external_queries=[],
            )

        # Complex questions → LLM decomposition (flash-lite)
        plan = await self._llm_decompose(question)
        return plan  # 2-4 internal + 0-2 external queries

DECOMPOSITION_PROMPT = """
You are a query decomposition specialist. Break down this question into
focused sub-queries that can be executed in parallel.

OUTPUT JSON:
{
    "internal_queries": [
        {"query": "specific search terms", "focus": "what this targets"}
    ],
    "external_queries": [
        {"query": "web search terms", "focus": "what to learn from web"}
    ]
}

RULES:
1. Generate 2-4 focused internal queries for different aspects
2. Generate 0-2 external queries ONLY if best practices / documentation
   comparison is needed
3. Internal queries should be keyword-focused (not full sentences)
4. If the question is simple, a single internal query suffices
"""
```

The decomposed sub-queries are then each routed independently through the Query Understanding step below, enabling parallel execution across both memory systems AND external search.

---

## 4.1 ADK Agent-Powered Query Understanding

Replaces the brittle regex classifier (weakness 1.10) with the `query_router_agent` (ADK `LlmAgent`, ~$0.001/query using flash-lite). The prompt below serves as the agent's system instruction:

```python
QUERY_UNDERSTANDING_PROMPT = """
Classify this query for a team communication knowledge base.

Query: {query}
Channel: {channel_name}

Determine:
1. route: One of:
   - "semantic": Looking for facts, discussions, topics, documents
     Examples: "What was discussed about auth?", "Find deployment docs", "Overview"
   - "graph": Looking for entity relationships, people, decisions, temporal changes
     Examples: "Who decided X?", "What is Alice working on?", "What blocks project Y?"
   - "both": Could benefit from both fact retrieval AND relationship context
     Examples: "Tell me about the JWT migration", "What happened with the auth project?"
2. semantic_depth: "overview" | "topic" | "detail" (for Weaviate tier routing)
3. entities: Named entities mentioned (people, projects, technologies)
4. topics: Topic areas referenced
5. temporal_scope: "recent" | "any" | "historical"
6. confidence: 0.0-1.0

Output JSON.
"""
```

---

## 4.2 Routing Strategy: Cost-Optimized

```
┌─────────────────────────────────────────────────────────────────────┐
│                       SMART QUERY ROUTER                             │
│                                                                      │
│  User Query                                                          │
│      │                                                               │
│      ▼                                                               │
│  ┌──────────────────────────────────────┐                           │
│  │  QUERY UNDERSTANDING (LLM flash-lite)│  ~$0.001/query            │
│  │                                      │                           │
│  │  route: semantic | graph | both      │                           │
│  │  semantic_depth: overview|topic|detail│                           │
│  │  entities: ["Alice", "JWT"]          │                           │
│  │  topics: ["authentication"]          │                           │
│  │  confidence: 0.0-1.0                 │                           │
│  └──────┬──────────┬──────────┬─────────┘                           │
│         │          │          │                                      │
│    route=semantic  │     route=both                                  │
│    conf > 0.7      │     OR conf ≤ 0.7                              │
│         │     route=graph    │                                      │
│         │     conf > 0.7     │                                      │
│         ▼          ▼         ▼                                      │
│  ┌──────────┐ ┌────────┐ ┌────────────────┐                       │
│  │ SEMANTIC │ │ GRAPH  │ │ BOTH PARALLEL  │                       │
│  │ ONLY     │ │ ONLY   │ │                │                       │
│  │          │ │        │ │ Semantic  Graph│                       │
│  │ Weaviate │ │ Neo4j  │ │ search + trav. │                       │
│  │ 3-tier   │ │ + Weav.│ │ in parallel   │                       │
│  │ retrieval│ │ enrich │ │                │                       │
│  │          │ │        │ │ Merge results  │                       │
│  │ $0.001   │ │ $0.005 │ │ $0.006        │                       │
│  │ < 200ms  │ │ ~500ms │ │ ~500ms        │                       │
│  └────┬─────┘ └───┬────┘ └───────┬────────┘                       │
│       │           │              │                                  │
│       │    ┌──────┘              │                                  │
│       │    │ Fallback: if graph  │                                  │
│       │    │ results insufficient│                                  │
│       │    │ → also run semantic │                                  │
│       │    │                     │                                  │
│       └────┴─────────┬───────────┘                                  │
│                      ▼                                               │
│  ┌──────────────────────────────────────┐                           │
│  │  RESULT MERGER + RESPONSE GENERATOR  │                           │
│  │                                      │                           │
│  │  1. Deduplicate by weaviate_id      │                           │
│  │  2. Boost cross-validated results   │                           │
│  │  3. Apply temporal decay            │                           │
│  │  4. Quality-score weighted ranking  │                           │
│  │  5. Generate grounded response      │                           │
│  │     via response_agent (ADK)        │                           │
│  └──────────────────────────────────────┘                           │
└─────────────────────────────────────────────────────────────────────┘
```

### Routing Decision Table

| Query Pattern | Route | Why | Cost | Latency |
|---|---|---|---|---|
| "What was discussed about auth?" | Semantic | Factual lookup → Weaviate excels | $0.001 | < 200ms |
| "Show me the overview" | Semantic (Tier 0) | Cached summary → FREE | $0 | < 50ms |
| "Tell me about deployment" | Semantic (Tier 1) | Topic cluster → FREE | $0 | < 50ms |
| "Find the architecture diagram" | Semantic (cross-modal) | Image search → Weaviate only | $0.001 | < 200ms |
| "Who decided to use JWT?" | Graph | Person→Decision traversal | $0.005 | ~500ms |
| "What is Alice working on?" | Graph | Person→Project traversal | $0.005 | ~500ms |
| "How did the auth approach evolve?" | Graph (temporal) | Decision→SUPERSEDES chain | $0.005 | ~500ms |
| "What blocks the migration?" | Graph | Project→BLOCKED_BY traversal | $0.005 | ~500ms |
| "Tell me about the JWT migration" | Both (parallel) | Needs facts AND relationships | $0.006 | ~500ms |
| "What happened with auth last week?" | Both (parallel) | Temporal + factual | $0.006 | ~500ms |

---

## 4.3 External Search (Tavily)

The v1 external search via Tavily is preserved in v2. It handles factual queries that require web knowledge (best practices, documentation, industry comparisons) — things NOT in the team's Slack history.

```python
class ExternalSearchService:
    """Web search via Tavily API for grounding with external knowledge.

    Why Tavily:
    - Cost-effective: 1,000 free credits/month vs $35/1K (Google)
    - Multiple tools: search, extract, crawl
    - No model restrictions: works with any LLM
    - Designed for AI/RAG: optimized for LLM consumption
    """

    async def search(self, query: str, search_depth: str = "basic",
                     max_results: int = 5, include_answer: bool = True,
                     include_domains: list[str] | None = None,
                     exclude_domains: list[str] | None = None,
                     ) -> ExternalSearchResponse:
        """Search the web. Returns results + optional AI-generated answer."""
        ...

    async def search_documentation(self, query: str,
                                    technology: str | None = None,
                                    max_results: int = 5,
                                    ) -> ExternalSearchResponse:
        """Optimized for finding API docs, tutorials, official docs."""
        ...

    async def extract_content(self, urls: list[str]) -> dict[str, str]:
        """Extract clean content from specific URLs."""
        ...
```

**Integration with Query Decomposition:**

When the `QueryDecomposer` produces `external_queries`, they are executed via Tavily in parallel with internal queries:

```
Complex Query → QueryDecomposer
  ├─ internal_queries → [routed to Semantic/Graph in parallel]
  └─ external_queries → [executed via Tavily in parallel]
      → Results merged into response context
```

**Routing decision:** The router classifies `external` queries via the decomposer, not via the query understanding LLM. Only queries that need web knowledge (comparisons, docs, best practices) generate external sub-queries.

| Config | Default |
|--------|---------|
| `TAVILY_API_KEY` | Required for external search |
| `ENABLE_EXTERNAL_SEARCH` | `true` |
| `TAVILY_SEARCH_DEPTH` | `"basic"` (1 credit) or `"advanced"` (2 credits) |
| `TAVILY_MAX_RESULTS` | `5` |

---

## 4.4 Graph Retrieval with Weaviate Enrichment

When the router selects Graph, Neo4j finds the relationships, then follows **episodic edges** back to Weaviate for the actual source text and citations:

```python
class GraphRetriever:
    """System-2: Neo4j traversal + Weaviate enrichment."""

    async def retrieve(self, query: str, channel_id: str | None,
                       understanding: QueryUnderstanding) -> list[dict]:

        # Step 1: Resolve entities from query to Neo4j nodes
        matched = await self.neo4j.fuzzy_match_entities(
            understanding.entities, channel_id
        )
        if not matched:
            return []  # No entities found → fallback to semantic

        # Step 2: Graph traversal (1-2 hops)
        if understanding.temporal_scope == "historical":
            paths = await self.neo4j.temporal_chain(matched[0], channel_id)
        else:
            paths = await self.neo4j.traverse(
                [m.name for m in matched], channel_id, max_hops=2
            )

        # Step 3: Follow episodic edges → get Weaviate memory IDs
        node_ids = self._extract_node_ids(paths)
        weaviate_ids = await self.neo4j.get_episodic_weaviate_ids(node_ids)

        # Step 4: Fetch full memories from Weaviate (text + citations)
        memories = await self.weaviate.fetch_by_ids(weaviate_ids)

        # Step 5: Combine graph structure + memory content
        return self._merge_graph_and_memories(paths, memories)
```

The episodic edge pattern is what makes graph queries grounded: Neo4j provides structure and relationships, but the actual text and citations always come from Weaviate. Neither store is queried in isolation for graph-routed requests.

> **ADK Implementation:** The `GraphRetriever` methods above are wrapped as ADK `FunctionTool` instances (`traverse_neo4j`, `temporal_chain`) on the `graph_agent` sub-agent. The Weaviate enrichment step uses `search_weaviate_hybrid`. See [`13-adk-integration.md`](13-adk-integration.md) for the full tool mapping.
