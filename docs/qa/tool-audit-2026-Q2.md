# QA Tool Audit — 2026 Q2

## Summary

The 10-tool QA pipeline is foundationally sound but uneven in documentation quality. Wiki tools (get_wiki_page, get_topic_overview) are well-specified; memory tools document latency and search semantics clearly. Graph tools lack concrete return-shape examples and have inconsistent branching behavior (dict vs list returns). External tools handle fallbacks gracefully. Priority: standardize graph tool docstrings and snapshot-test the list/dict branching in the citation decorator before Stream 3b refactors.

## Scoring key

- **Description clarity (0-5)**: How well the docstring tells the LLM when/why to use this tool, with concrete use cases
- **Example count**: Number of explicit usage examples in the docstring (inline calls, mock returns, scenarios)
- **Return-schema completeness (0-5)**: How thoroughly the return shape is specified (typed fields, optional keys, sentinel values)
- **Latency profile**: Rough p50 from docstring or implementation (wiki <50ms cache; memory ~100-200ms Weaviate; graph ~500ms Neo4j; external ~1s Tavily)
- **Known failure modes**: 1-3 documented or inferred edge cases

## Scored tools table

| Tool | Module | Desc (0-5) | Examples | Schema (0-5) | Latency | Failure modes |
|------|--------|-----------|----------|-------------|---------|---------------|
| get_wiki_page | wiki_tools | 4 | 0 | 4 | <50ms | stale cache sentinel; page_type validation; missing channel |
| get_topic_overview | wiki_tools | 4 | 0 | 4 | <50ms | tier fallback logic; topic fuzzy-match; no clusters |
| search_qa_history | memory_tools | 4 | 0 | 4 | <100ms | embedding failure fallback; qa_history_negative_filter; empty channel |
| search_channel_facts | memory_tools | 5 | 0 | 5 | <200ms | hybrid search fallback; time_scope cutoff; MMR diversity tuning |
| search_media_references | memory_tools | 4 | 0 | 4 | <200ms | media_type filtering; empty results; link/pdf detection |
| get_recent_activity | memory_tools | 4 | 0 | 4 | <200ms | timestamp parsing; time window cutoff; topic optional filter |
| search_relationships | graph_tools | 3 | 0 | 2 | ~500ms | returns dict vs list (branching); entity fuzzy-match; empty graph |
| trace_decision_history | graph_tools | 3 | 0 | 2 | ~500ms | returns list vs dict (inconsistent); SUPERSEDES edge detection; exception path returns dict not list |
| find_experts | graph_tools | 3 | 0 | 2 | ~500ms | list_relationships semantic scoring; _empty sentinel; token filtering |
| search_external_knowledge | external_tools | 5 | 0 | 5 | ~1s | tavily_unavailable (env var); tavily not installed; search timeout |

## Per-tool detailed findings

### Wiki tools

#### get_wiki_page

**Docstring** (lines 17-28):
```
Retrieve a pre-compiled wiki page from MongoDB wiki_cache.

Cost: $0. Target latency: <50ms (cache read only, no Weaviate/Neo4j queries).

Args:
    channel_id: The channel to look up.
    page_type: One of: overview, faq, decisions, people, glossary, activity, topics.

Returns:
    Dict with page_type, content (markdown), and summary — or None if unavailable.
```

**What's good:**
- Clear cost and latency expectations
- Comprehensive page_type enumeration (7 types defined in SUPPORTED_PAGE_TYPES, line 11)
- Graceful None return for missing pages
- Smart fallback: detects stale activity sentinel and calls get_recent_activity (line 46-66)

**What's weak:**
- Docstring does not document the stale activity sentinel retry logic — callers won't know this tool can invoke memory_tools
- Return dict keys undocumented: `content`, `summary`, `text` fields all exposed but not in docstring
- `_cite_tool_output(kind="wiki_page")` decorator dependency not mentioned

**Proposed improvements:**
- Expand Returns section: `Dict with page_type, channel_id, content (markdown), summary (text), text (excerpt for citation decorator). For stale activity pages, returns synthesized result from get_recent_activity.`
- Note that `text` field is the citation decorator's grounding field (line 75)

#### get_topic_overview

**Docstring** (lines 85-98):
```
Retrieve channel-level summary (Tier 0) or a topic cluster summary (Tier 1).

Cost: $0 (cached). Target latency: <50ms.

Args:
    channel_id: The channel to look up.
    topic_name: Optional topic to narrow to a matching Tier 1 cluster.

Returns:
    Dict with tier, summary, and metadata — or None if unavailable.
```

**What's good:**
- Tier 0/1 distinction is clear and well-scoped
- Explicit Tier 0 vs Tier 1 branching (lines 104-116 vs 118-140)
- Fuzzy-match logic handles topic_name lookups gracefully (line 123)

**What's weak:**
- Return schema is sparse: mentions tier/summary/metadata but actual keys are tier, channel_id, page_type, summary, text, cluster_count, fact_count, slug, cluster_id, topic_tags, member_count
- Fallback to clusters[0] when topic_name doesn't match (line 127) is not documented

**Proposed improvements:**
- Expand Returns: `Tier 0: {tier: "summary", channel_id, page_type: "overview", summary, text, cluster_count, fact_count}. Tier 1: {tier: "topic", channel_id, page_type: "topics", slug, cluster_id, summary, text, topic_tags, member_count}.`

### Memory tools

#### search_qa_history

**Docstring** (lines 54-67):
```
Search past Q&A pairs semantically for similar questions in this channel.

Cost: $0. Target latency: <100ms.

Args:
    channel_id: Scope search to this channel.
    query: Search query.
    limit: Max results.

Returns:
    List of past Q&A entries with question, answer, citations, timestamp.
```

**What's good:**
- Cost/latency explicit
- Embedding failure fallback documented in code (line 78) with bm25 fallback
- QA_HISTORY_NEGATIVE_FILTER applied post-search (line 84)

**What's weak:**
- Return dict shape is vague ("question, answer, citations, timestamp") — actual shape from store.search_qa_history() is not described
- Citation decorator dependency hidden

**Proposed improvements:**
- Note: `Returns list of dicts from QAHistoryStore.search_qa_history(), decorated by cite_tool_output(kind="qa_history"). If embedding fails, falls back to BM25. Results filtered by qa_history_negative_filter if configured.`

#### search_channel_facts

**Docstring** (lines 150-171):
```
BM25 keyword search over atomic facts (Weaviate Tier 2 / tier=atomic).

Cost: ~$0.001. Target latency: <200ms.

Results are MMR re-ranked (λ≈0.6) to improve diversity when multiple
paraphrased queries hit the same top facts.

Args:
    channel_id: Scope to this channel.
    query: Search query.
    time_scope: "recent" (last 30 days) or "any".
    limit: Max results.

Returns:
    Ranked facts with author, channel, timestamp, permalink, confidence.
```

**What's good:**
- Excellent docstring: explains cost, latency, MMR algorithm (λ=0.6), and time_scope semantics
- Hybrid search with vector fallback documented in code (lines 181-196)
- Rich return dict: author, author_id, channel_id, channel_name, platform, timestamp, permalink, importance, confidence, fact_id, topic_tags, media_urls, link_urls (lines 212-230)

**What's weak:**
- Return docstring says "Ranked facts" but doesn't detail the 14 dict keys
- MMR re-rank is described in docstring but not all keys returned by the internal store are documented

**Proposed improvements:**
- Expand Returns: `List[{text, author, author_id, channel_id, channel_name, platform, message_ts, timestamp (ISO), permalink, importance, confidence (0-1), fact_id, topic_tags, media_urls, link_urls}]. Over-fetched (k*3, capped 30), then MMR re-ranked down to limit.`

#### search_media_references

**Docstring** (lines 248-266):
```
Search for images, PDFs, and links shared in the channel.

Cost: ~$0.001. Target latency: <200ms.

Args:
    channel_id: Scope to this channel.
    query: Search query.
    media_type: "image", "pdf", "link", or None for all.
    limit: Max results.

Returns:
    Media items with URL, type, and surrounding message context.
```

**What's good:**
- Clear media_type enum and None behavior
- Hybrid search with fallback (lines 272-288)
- Post-filter by media_type (lines 296-303)

**What's weak:**
- Return dict keys not listed (actual: text, media_urls, link_urls, link_titles, author, channel_id, channel_name, platform, message_ts, timestamp, media_type, fact_id)
- PDF detection is heuristic-based (line 294: `.pdf` substring match) — not documented

**Proposed improvements:**
- Expand Returns: `List[{text, media_urls, link_urls, link_titles, author, channel_id, channel_name, platform, message_ts, timestamp (ISO), media_type, fact_id}]. Filtered by media_type if specified.`

#### get_recent_activity

**Docstring** (lines 328-346):
```
Return recent facts from the channel, optionally filtered by topic.

Cost: $0. Target latency: <200ms.

Args:
    channel_id: Scope to this channel.
    days: How many days back to look.
    topic: Optional topic filter.
    limit: Max results.

Returns:
    Facts from the last N days ordered by timestamp descending.
```

**What's good:**
- Clear time window (days parameter, default 7)
- Optional topic filter
- Explicit sort order (timestamp descending, line 395)

**What's weak:**
- Return dict schema not documented (actual: text, author, author_id, channel_id, channel_name, platform, message_ts, timestamp, importance, topic_tags, fact_id; lines 379-391)
- Hybrid search with fallback not obvious from docstring

**Proposed improvements:**
- Expand Returns: `List[{text, author, author_id, channel_id, channel_name, platform, message_ts, timestamp (ISO), importance, topic_tags, fact_id}] ordered by timestamp descending.`

### Graph tools

#### search_relationships

**Docstring** (lines 14-30):
```
Traverse Neo4j graph for relationships between named entities.

Cost: ~$0.005. Target latency: ~500ms.

Args:
    channel_id: Scope traversal context (used for logging/filtering).
    entities: List of entity names to resolve and traverse from.
    hops: Number of graph hops (default 2).

Returns:
    Dict with nodes, edges, and entities_searched.
```

**What's good:**
- Clear hops semantics
- Fuzzy-match fallback (line 42)

**What's weak:**
- Return type is documented as "Dict" but actual shape is inconsistent: returns dict on success (lines 87-97) but list[dict] on empty graph (line 80)
- Empty-graph case returns `[{"_empty": True, ...}]` (a list), violating the docstring contract
- Keys in success dict not itemized (actual: entities_searched, nodes, edges, text, subject_id, predicate, object_id, channel_id)
- Node shape (`{name, type}`) and edge shape (`{source, target, type, confidence, context}`) not documented

**Proposed improvements (non-breaking):**
- Docstring: `Returns dict {entities_searched: List[str], nodes: List[{name, type}], edges: List[{source, target, type, confidence, context}], text (excerpt), subject_id, predicate, object_id, channel_id}. On empty graph, returns dict with empty nodes/edges lists (not a list).`
- Consider removing the `[{"_empty": True}]` branch (line 80) to guarantee dict return type.

#### trace_decision_history

**Docstring** (lines 104-115):
```
Trace temporal evolution of decisions about a topic via Neo4j SUPERSEDES chain.

Cost: ~$0.005. Target latency: ~500ms.

Args:
    channel_id: Scope context (for logging).
    topic: Topic or entity name to trace.

Returns:
    List of decision nodes and SUPERSEDES relationships, ordered by traversal.
```

**What's good:**
- Clear relationship type (SUPERSEDES)
- Topic fuzzy-match (line 122)

**What's weak:**
- Return type is documented as `List[dict]` but exception handler at line 166 returns `{"result": [], "error": "graph_unavailable"}` — a dict, not a list
- No example of success shape (actual: entity, superseded_by, relationship, confidence, context, text, decision_id, channel_id, topic)
- Sentinel return (line 157: `[{"_empty": True, ...}]`) is inconsistent with normal list returns

**Proposed improvements (critical):**
- Fix exception return (line 166) to return `[]` or `[{"_empty": True, ...}]` for consistency
- Docstring: `Returns List[{entity, superseded_by, relationship: "SUPERSEDES", confidence, context, text (excerpt), decision_id, channel_id, topic}]. Empty graph returns [{"_empty": True, ...}].`

#### find_experts

**Docstring** (lines 173-185):
```
Find top contributors for a topic by Neo4j expertise ranking.

Cost: ~$0.005. Target latency: ~500ms.

Args:
    channel_id: Scope to this channel.
    topic: Topic to rank expertise for.
    limit: Max people to return (default 5).

Returns:
    List of {handle, expertise_score, fact_count} ordered by expertise_score desc.
```

**What's good:**
- Clear ranking order (expertise_score desc, line 209)
- Fallback for no results (line 211)

**What's weak:**
- Return shape incomplete: actual dict also includes text, subject_id, predicate, object_id, channel_id (lines 215-219)
- Semantic scoring logic (lines 199-207) is opaque: scores any Person endpoint connected to a topic-containing node — not described in docstring

**Proposed improvements:**
- Expand Returns: `List[{handle, expertise_score, fact_count, text (excerpt), subject_id, predicate: "EXPERT_IN", object_id, channel_id}] ordered by expertise_score desc. Empty results return [{"_empty": True, ...}].`

### Citation decorator touchpoints

The citation decorator (`_citation_decorator.py:68-89`) has three branching paths:

1. **List return (line 68-75)**: `isinstance(result, list)` — iterates each dict, annotates with `_cite` and `_src_id`
   - Tools: `search_qa_history`, `search_channel_facts`, `search_media_references`, `get_recent_activity`, `trace_decision_history` (mostly), `find_experts`
   - Sentinel handling: skips dicts with `_empty: True` (line 72)

2. **Dict envelope (line 77-86)**: `isinstance(result, dict)` → checks for `results`, `items`, or `data` key containing a list
   - Tools: `search_external_knowledge` (returns dict with `results` key; line 74 in external_tools.py)
   - Unwraps, annotates inner list, re-wraps

3. **Single-source dict (line 87-89)**: Falls through to annotate whole dict
   - Tools: `search_relationships`, `get_wiki_page`, `get_topic_overview` (both return single dict per call)

**Inconsistencies requiring snapshot tests before refactor:**

- `search_relationships` returns dict on success but `[{"_empty": True}]` (list) on empty graph → inconsistent with decorator's list-vs-dict detection
- `trace_decision_history` returns `list[dict]` on success but `dict` (error shape) on ConnectionError (line 166) → requires snapshot to verify decorator doesn't crash
- `get_wiki_page` and `get_topic_overview` return single dict but decorator treats them as single-source (path 3), not envelope-wrapped — correct, but not obvious

**Recommendation:** Generate snapshot tests for all three paths before Stream 3b refactors the decorator or return shapes.

## Proposed TypedDict shapes for graph_tools.py (Stream 3b input)

### search_relationships → dict (return type, not list)

```python
class RelationshipNode(TypedDict):
    name: str
    type: str | None

class RelationshipEdge(TypedDict):
    source: str
    target: str
    type: str
    confidence: float
    context: str

class RelationshipSearchResult(TypedDict):
    entities_searched: list[str]
    nodes: list[RelationshipNode]
    edges: list[RelationshipEdge]
    text: str  # citation decorator field
    subject_id: str  # citation decorator field
    predicate: str  # citation decorator field
    object_id: str  # citation decorator field
    channel_id: str  # citation decorator field
```

### trace_decision_history → list[dict]

```python
class DecisionEvent(TypedDict):
    entity: str
    superseded_by: str
    relationship: str  # always "SUPERSEDES"
    confidence: float
    context: str
    text: str  # citation decorator field
    decision_id: str  # citation decorator field
    channel_id: str  # citation decorator field
    topic: str
```

### find_experts → list[dict]

```python
class ExpertHit(TypedDict):
    handle: str
    expertise_score: float
    fact_count: int
    text: str  # citation decorator field
    subject_id: str  # citation decorator field
    predicate: str  # "EXPERT_IN"
    object_id: str  # citation decorator field
    channel_id: str  # citation decorator field
```

## Refactor recommendations deferred (non-graph modules)

**wiki_tools.py**: Docstring enhancements only (no code changes). The stale activity sentinel and fallback to get_recent_activity (line 46-66) should be documented in the Returns section and hinted in the Args as "may invoke memory_tools if page is stale."

**memory_tools.py**: Expand all return dict schemas in docstrings to list actual keys. The _mmr_rerank helper (line 92-143) is well-commented; no changes needed. The embedding fallback patterns (search_qa_history:78, search_channel_facts:189) are consistent and should be noted in docstrings.

**external_tools.py**: Already well-documented. The envelope-wrapped result (lines 59-77) is appropriate for Tavily API responses. No changes needed.

## Golden query set for Stream 3b snapshot tests

Use these representative queries to generate JSON snapshots before refactoring graph_tools or citation decorator:

1. **search_relationships, single entity**: `search_relationships(channel_id="C001", entities=["Alice"], hops=2)` → should return dict with 5+ edges, verify `text` field is present
2. **search_relationships, multi-entity**: `search_relationships(channel_id="C001", entities=["Alice", "Bob"], hops=1)` → should merge subgraphs, verify no duplicate nodes
3. **search_relationships, empty**: `search_relationships(channel_id="C001", entities=["Nonexistent"], hops=2)` → should return dict with empty nodes/edges (not list)
4. **trace_decision_history, with SUPERSEDES**: `trace_decision_history(channel_id="C001", topic="Architecture v2")` → should return list of 2-5 events, verify decision_id format
5. **trace_decision_history, no decisions**: `trace_decision_history(channel_id="C001", topic="Nonexistent")` → should return list or empty sentinel, verify not dict
6. **find_experts, high scoring**: `find_experts(channel_id="C001", topic="Database", limit=3)` → should return 1-3 dicts, verify expertise_score is numeric
7. **find_experts, no experts**: `find_experts(channel_id="C001", topic="Obscure XYZ", limit=5)` → should return empty list or sentinel
8. **search_channel_facts, MMR rerank**: `search_channel_facts(channel_id="C001", query="deployment", limit=5)` → verify over-fetch (k*3) is applied and results are diversity-ranked
9. **get_wiki_page, stale activity**: `get_wiki_page(channel_id="C001", page_type="activity")` → if cached page contains stale sentinel, verify fresh fallback is invoked
10. **search_external_knowledge, Tavily envelope**: `search_external_knowledge(query="Python best practices", mode="best_practices")` → verify `results` key is unwrapped by decorator before annotation
