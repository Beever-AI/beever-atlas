# Beever Atlas — Retrieval System Improvement Ideas

> **Date**: March 20, 2026
> **Status**: Design exploration — validated weaknesses with proposed solutions
> **Scope**: Hierarchical memory retrieval, query classification, consolidation, and search quality
>
> **How to use this doc**: Check boxes `[x]` to mark items as agreed/done. Add discussion notes inline under each item.

---

## Table of Contents

1. [Validated Weaknesses](#1-validated-weaknesses)
2. [Proposed Solutions](#2-proposed-solutions)
3. [Implementation Roadmap](#3-implementation-roadmap)

---

## Quick Decision Checklist

Use this during team discussions to quickly mark decisions:

**Weaknesses — Do we agree these are real?**
- [ ] 1.1 Top-down only retrieval
- [ ] 1.2 Meaningless expansion thresholds
- [ ] 1.3 Detail queries bypass hierarchy (HIGH)
- [ ] 1.4 Temporal decay never applied (HIGH)
- [ ] 1.5 No feedback loop
- [ ] 1.6 Single workspace / Slack only
- [ ] 1.7 No real-time sync
- [ ] 1.8 No memory expiration
- [ ] 1.9 ADK migration incomplete
- [ ] 1.10 Brittle regex query classifier
- [ ] 1.11 Cluster linking is a no-op (HIGH — blocker)
- [ ] 1.12 No cross-channel search
- [ ] 1.13 Memory quality 5.25/10 (HIGH)
- [ ] 1.14 No adaptive alpha in hierarchical retrieval
- [ ] 1.15 No semantic dedup across tiers

**Solutions — What do we want to build?**
- [ ] A: Two-stage topic-first retrieval
- [ ] B: Bidirectional tier expansion
- [ ] C: Score-based expansion thresholds
- [ ] D: Apply temporal decay (1-line fix)
- [ ] E: LLM-augmented query classification
- [ ] F: Memory quality pipeline
- [ ] G: Adaptive alpha per query (1-line fix)
- [ ] H: Cross-tier semantic dedup

**Phases — What are we committing to?**
- [ ] Phase 1: Quick wins (D, G, C) — 1-2 days
- [ ] Phase 2: Consolidation fix — 2-3 days
- [ ] Phase 3: Retrieval redesign (A, B, E, H) — 1-2 weeks
- [ ] Phase 4: Quality & ecosystem (F, feedback, cross-channel) — 2-4 weeks

**Discussion Notes:**
> _Add team discussion notes, decisions, and open questions here._
>
> _Date:_ _______________
>
> _Participants:_ _______________
>
> _Key decisions:_
>
>
>
> _Open questions:_
>
>
>
> _Next steps:_
>
>

---

## 1. Validated Weaknesses

Each weakness below has been verified against the codebase with specific file/line references.

### 1.1 Top-Down Only Retrieval (No Bottom-Up) (Need to study)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Medium | File: `services/hierarchical_retrieval.py:170-203`**

The `HierarchicalRetrievalService.retrieve()` only expands **downward**:

```
Tier 0 (sparse?) → expand to Tier 1
Tier 1 (sparse?) → expand to Tier 2
Tier 2           → stop (no upward path)
```

There is no upward path. If a detail query at Tier 2 returns weak results, the system cannot navigate up to a parent cluster for broader context. This is a one-way escalation that can only get more granular, never more abstract.

**Why this matters:** When a user asks "What's the overall project status?", the system goes to Tier 0. If the Tier 0 summary is stale (only updated during consolidation), the user gets outdated information. A bottom-up approach would start from Tier 2 (always the freshest data), group by topic dynamically, and synthesize a live overview.

**Evidence:** The `retrieve()` method has three branches — all three can only call downward:
- `depth == "summary"` → may expand to `_retrieve_clusters`
- `depth == "cluster"` → may expand to `_retrieve_atomic`
- `depth == "atomic"` → terminal, no expansion

### 1.2 Hardcoded Expansion Thresholds Are Meaningless (Need to study)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Medium | File: `services/hierarchical_retrieval.py:176, 191`**

The expansion logic uses arbitrary count thresholds:

```python
# Summary → Cluster expansion
if len(memories) < 2:    # Why 2?
    cluster_memories = await self._retrieve_clusters(...)

# Cluster → Atomic expansion
if len(memories) < 3:    # Why 3?
    atomic_memories = await self._retrieve_atomic(...)
```

These are raw result **counts**, not relevance **scores**. A search could return 5 results that are all irrelevant (low similarity scores), and the system considers that "enough" and skips expansion. Conversely, a search that returns 1 highly relevant result would trigger expansion unnecessarily, diluting the context.

**What should be measured instead:**
- **Best result confidence**: Is the top result actually relevant? (`score > 0.7`?)
- **Average quality**: Is the result set overall useful?
- **Coverage**: Does the result set actually address the query's intent?

### 1.3 Detail Queries Don't Benefit from Hierarchical Structure (Need to study)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: High | File: `services/hierarchical_retrieval.py:199-203`**

When a query is classified as `DETAIL`, it goes straight to a flat Tier 2 search across **all** atomic memories for the channel:

```python
else:  # atomic
    memories = await self._retrieve_atomic(
        channel_id, query, topic_filter, action_filter, max_results
    )
```

This is identical to a flat vector search — the 3-tier hierarchy provides **zero benefit** for detail queries. For a channel with 10,000 atomic memories, the search scans all of them with no topic scoping.

**The better approach:** First identify relevant topic clusters (fast, small search space), then search atomic memories *within those clusters* using `member_ids` as a filter. This narrows the search space from thousands to tens, improving both precision and speed.

### 1.4 Temporal Decay Exists But Is Never Applied to Retrieval Ranking (Need to study)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: High | Files: `services/temporal.py:153-181`, `services/query.py:269`, `services/grounding.py:82`**

The `TemporalResolutionService` has a well-designed `apply_temporal_decay()` method that computes:

```python
decay_factor = math.exp(-self.decay_rate * (days_ago / 30))
score = original_score * decay_factor
```

**However, this method is never called anywhere in production code.** The only temporal method actually used is `enrich_memories_with_temporal()`, which adds text labels ("2 days ago", "Last week") but **does not affect ranking**.

Call sites that use the label-only method (no score adjustment):
- `query.py:269` — `self.temporal.enrich_memories_with_temporal(memories)`
- `grounding.py:82` — `self.temporal_service.enrich_memories_with_temporal(limited_memories)`
- `hierarchical_retrieval.py` — never calls temporal service at all

**Impact:** A decision from 6 months ago has **identical retrieval weight** as one from yesterday. The temporal context only appears as text labels in the LLM generation prompt, relying entirely on the LLM to reason about recency — which is unreliable and inconsistent.

### 1.5 No Feedback Loop for Retrieval Quality (No need first)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Medium | File: `docs/architecture/09-MEMORY_EVAL_PLAN.md`**

There is no mechanism for:
- User thumbs up/down on answers
- Tracking which retrieved memories were actually cited in final responses
- Learning from bad retrievals to improve future ranking
- Active learning for relevance tuning

The eval plan document (`09-MEMORY_EVAL_PLAN.md`) proposes metrics like Precision@K, Recall@K, and MRR, but these are **documentation only** — no evaluation pipeline runs in production. The eval framework code is in the doc as examples, not as shipped code.

### 1.6 Single Workspace, Slack Only (Can see ChatSDK -> See ChatSDK documentation)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Medium | File: `docs/reference/01-standalone-mcp-server-plan.md:1533-1534`**

The system is hardcoded to a single Slack workspace. The roadmap explicitly lists multi-workspace support as a v1.2 feature (not implemented). No support for Microsoft Teams, Discord, or other communication platforms.

### 1.7 No Real-Time Sync (Need to discuss)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Medium | Files: `config.py:17-18`, `docs/reference/01-standalone-mcp-server-plan.md:1540`**

Sync is pull-based (triggered manually via `sync_channel()`). The `slack_app_token` and `slack_signing_secret` config fields exist but are unused — Socket Mode was planned but never wired into the sync pipeline. The roadmap lists "Slack Events API integration (real-time)" as a v2.0 feature.

### 1.8 No Memory Expiration / Storage Growth Management (Not a big problem, but focus on the query and retrieval and matching score first)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Medium | File: `services/consolidation.py`**

Architecture docs describe a `monthly` consolidation type that would `archive_old_memories`, but the actual implementation only supports `daily`, `weekly`, and `full` — **no archival or expiration logic exists**.

Manual deletion is possible via API (`DELETE /memories`, `DELETE /channels/{channel_id}/memories`), but there is no automated TTL, importance-based pruning, or storage growth management. Over time, channels accumulate unbounded memories with no decay.

### 1.9 ADK Migration Incomplete

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Low | File: `docs/architecture/11-ADK_MIGRATION_PLAN.md`**

The migration plan from direct `google.genai` SDK calls to Google ADK agent architecture is documented but only partially executed. The `agents/` directory has scaffolding (coordinator, orchestrator, retrieval agents), but the MCP tools in `server.py` still use the services path for primary operations.

### 1.10 Query Classification Uses Brittle Regex, Not LLM (need to do)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Medium | File: `services/hierarchical_retrieval.py:49-120`**

The `QueryClassifier` uses hardcoded regex patterns:

```python
OVERVIEW_PATTERNS = [r"what.*happening", r"summarize", r"overview", ...]
TOPIC_PATTERNS = [(r"about\s+(?:the\s+)?(\w+)", 1), ...]  # Single word only!
DETAIL_PATTERNS = [r"who\s+said", r"when\s+did", r"yesterday", ...]
```

Problems:
- **Single-word topic capture**: `(\w+)` only captures one word — "API design" becomes just "API"
- **Priority misclassification**: DETAIL patterns are checked first, so "who said something about authentication" matches `r"who\s+said"` and is classified as DETAIL, skipping topic extraction entirely
- **No LLM fallback**: `model_query_classification = gemini-2.5-flash-lite` is configured in `config.py:27` but **never used** by the classifier
- **No multi-topic detection**: "Tell me about NBA and FIFA" would be classified as TOPIC_SPECIFIC with topic="NBA" only, completely missing "FIFA"

### 1.11 Cluster Linking Is a No-Op (Blocks Topic-First Retrieval)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: High | File: `services/consolidation.py:214-231`**

When atomic memories are consolidated into clusters, `_link_memories_to_cluster()` should update atomic memories with their `cluster_id`. But it's implemented as a no-op:

```python
async def _link_memories_to_cluster(self, memories, cluster_id):
    # In a future version, we could update memories in Weaviate
    logger.debug(f"Linked {len(memories)} memories to cluster {cluster_id}")
```

**This is the single biggest blocker for retrieval improvement.** Without cluster linkage:
- Atomic memories don't know which cluster they belong to
- The "topic → atomic" filtered retrieval path is impossible
- Every consolidation run re-selects the same memories (they never get marked as clustered)
- Duplicate clusters accumulate indefinitely

### 1.12 No Cross-Channel Search (Can purpose, lower priority)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Medium | Files: `server.py` (all tools), `api/chat_routes.py`**

Every query tool requires a `channel_id` parameter. There is no way to search across multiple channels simultaneously. If a decision about authentication was discussed in `#backend` but the user asks in `#frontend`, it won't be found.

### 1.13 Memory Quality Is Low (5.25/10 Average) (Graph Related Memory maybe can fix, can see the open source project, DeepWiki MCP can help - Claude Code operate related repo to study)

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: High | File: `docs/architecture/09-MEMORY_EVAL_PLAN.md:35`**

The eval plan documents significant quality problems from a 319-memory audit:

| Metric | Value | Target |
|--------|-------|--------|
| Average quality score | 5.25/10 | >7.0 |
| Facts per message | 2.44 | 1.5-2.0 |
| High quality (>6) | 2.2% | >50% |
| Vague/generic | 17% | <5% |

Problematic memory examples:
- "The user does not use 'uv'." (no context — what is this about?)
- "The output was adjusted accordingly." (what output? how?)
- "The process runs through all steps." (what process?)

These low-quality memories pollute the retrieval index, causing the system to return vague facts instead of actionable information. **Garbage in, garbage out** — even a perfect retrieval system would struggle with this quality level.

### 1.14 No Per-Query-Type Hybrid Alpha Tuning

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Low | Files: `services/weaviate_client.py:318-354`, `services/hierarchical_retrieval.py:238,265,295`**

A `get_adaptive_alpha()` function exists in `weaviate_client.py` that adjusts alpha based on query length:
- 1-2 short words → `alpha=0.2` (favor BM25)
- 1-2 long words → `alpha=0.35`
- 3-5 words → `alpha=0.5`
- 6+ words → `alpha=0.7` (favor vector)

**However, the hierarchical retrieval service bypasses this.** It uses:
- Tier 0 summaries: hardcoded `alpha=0.3`
- Tier 1 clusters: `settings.hybrid_alpha` (default 0.6)
- Tier 2 atomic: `settings.hybrid_alpha` (default 0.6)

The adaptive alpha is only used in the raw `hybrid_search` function when no alpha is explicitly passed. Since the hierarchical retrieval always passes an explicit alpha, the adaptive logic never runs for hierarchical queries.

### 1.15 No Semantic Deduplication Across Tiers

- [ ] **Agreed** | - [ ] **Prioritized** | - [ ] **Fixed**

**Severity: Low | File: `services/hierarchical_retrieval.py:206-213`**

When retrieval expands from one tier to another (e.g., cluster → atomic), deduplication only checks by memory ID:

```python
mem_id = m.get("id") or m.get("memory", "")[:50]
if mem_id not in seen_ids:
    seen_ids.add(mem_id)
    unique_memories.append(m)
```

But the same information can exist in both a Tier 1 cluster summary and its constituent Tier 2 atomic memories. For example:
- **Tier 1 cluster**: "The team decided to use JWT with RS256 for authentication"
- **Tier 2 atomic**: "Team chose RS256 algorithm for JWT signing"

These are semantically identical but have different IDs, so both get included in the LLM context, wasting token budget and potentially confusing the response generator.

---

## 2. Proposed Solutions

### Solution A: Two-Stage Topic-First Retrieval

- [ ] **Approved** | - [ ] **In Progress** | - [ ] **Done**

**Addresses: #1.1, #1.3, #1.11**

Replace the flat "pick a tier and search" approach with a two-stage coarse-to-fine retrieval:

```
Stage 1: Topic Identification (coarse)
  Input:  "What did the team decide about JWT authentication?"
  Action: hybrid_search(tier="tier1_cluster", query=question, limit=5)
  Output: Matched clusters with their member_ids
          → "authentication" cluster (members: [uuid1, uuid2, ..., uuid15])
          → "security" cluster (members: [uuid20, uuid21, ..., uuid28])

Stage 2: Focused Retrieval (fine)
  Input:  member_ids from matched clusters
  Action: hybrid_search(ids=member_ids, query=question, limit=10)
  Output: Precise results from a narrowed search space (43 memories instead of 10,000)
```

**Why this is better than current approach:**

| Aspect | Current (flat Tier 2) | Topic-First |
|--------|----------------------|-------------|
| Search space | All atomic memories | Only cluster members |
| Precision | Low — irrelevant topics compete | High — pre-filtered by topic |
| Scalability | Degrades with channel size | Bounded by cluster size |
| Context coherence | Mixed topics in results | Topically focused |

**Prerequisites:**
1. Fix `_link_memories_to_cluster()` to actually set `cluster_id` on atomic memories (Weakness #1.11)
2. Add a Weaviate filter for `cluster_id IN [...]` or use `member_ids` list lookup

**Estimated effort:** Medium (after consolidation fix)

### Solution B: Bidirectional Tier Expansion

- [ ] **Approved** | - [ ] **In Progress** | - [ ] **Done**

**Addresses: #1.1, #1.3**

Allow retrieval to expand both downward (current) and upward:

```python
async def retrieve(self, channel_id, query, depth="auto", max_results=20):
    memories = await self._search_at_depth(depth, ...)

    # Current: only downward expansion
    if self._should_expand(memories, direction="down"):
        expanded = await self._search_deeper(...)
        memories.extend(expanded)

    # NEW: upward expansion when detail results are weak
    if self._should_expand(memories, direction="up"):
        broader = await self._search_broader(...)
        memories = self._merge_and_rerank(memories, broader)
```

**Upward expansion scenarios:**
- Detail query returns 0 results → expand to cluster for topic summary
- Detail query returns low-confidence results → check if a cluster summary answers the question directly
- Overview query with stale Tier 0 → synthesize from fresh Tier 2 atomics (bottom-up)

**Estimated effort:** Low-Medium

### Solution C: Score-Based Expansion Thresholds

- [ ] **Approved** | - [ ] **In Progress** | - [ ] **Done**

**Addresses: #1.2**

Replace magic count thresholds with relevance-score-based decisions:

```python
def _should_expand(self, memories: list[dict], direction: str) -> bool:
    """Decide whether to expand to adjacent tier based on result quality."""
    if not memories:
        return True  # No results — always expand

    scores = [m.get("score", 0) for m in memories]
    max_score = max(scores)
    avg_score = sum(scores) / len(scores)

    # Expand if best result is low-confidence
    if max_score < self.expansion_score_threshold:  # configurable, e.g., 0.6
        return True

    # Expand if overall quality is poor
    if avg_score < self.expansion_avg_threshold:  # configurable, e.g., 0.4
        return True

    # Don't expand if we have good results
    return False
```

After expansion, **re-rank the combined results** by score instead of just appending:

```python
all_memories = original + expanded
all_memories.sort(key=lambda m: m.get("score", 0), reverse=True)
return all_memories[:max_results]
```

**Estimated effort:** Low

### Solution D: Apply Temporal Decay to Retrieval Ranking

- [ ] **Approved** | - [ ] **In Progress** | - [ ] **Done**

**Addresses: #1.4**

Wire the existing `apply_temporal_decay()` method into the retrieval pipeline:

```python
# In HierarchicalRetrievalService.retrieve(), before returning:
if settings.temporal_decay_rate > 0:
    self.temporal_service.apply_temporal_decay(result.memories)
```

This single line change would:
- Boost recent memories (decay_factor ≈ 1.0 for today)
- Penalize old memories (decay_factor ≈ 0.9 for 1 month, ≈ 0.74 for 3 months)
- Re-sort results so recency-weighted relevance determines order

The decay rate is already configurable via `temporal_decay_rate` (default 0.1 = 10% per 30 days).

**Estimated effort:** Very low (1 line + import)

### Solution E: LLM-Augmented Query Classification

- [ ] **Approved** | - [ ] **In Progress** | - [ ] **Done**

**Addresses: #1.10**

Replace the regex classifier with a hybrid approach:

```python
class QueryClassifier:
    async def classify(self, query: str) -> tuple[QueryType, dict]:
        # Fast path: clear-cut queries via regex (zero cost)
        regex_result = self._regex_classify(query)
        if regex_result.confidence > 0.9:
            return regex_result

        # Slow path: ambiguous queries via LLM (flash-lite, ~0.001$/call)
        return await self._llm_classify(query)

    async def _llm_classify(self, query: str) -> tuple[QueryType, dict]:
        """Use the already-configured model_query_classification."""
        prompt = f"""Classify this query and extract topics:
        Query: {query}

        Output JSON:
        {{"type": "overview|topic|detail", "topics": ["topic1", "topic2"], "temporal": "recent|any"}}
        """
        response = self.gemini.models.generate_content(
            model=settings.model_query_classification,  # gemini-flash-lite
            contents=prompt,
        )
        return self._parse_classification(response.text)
```

**Key improvements over pure regex:**
- Multi-word topic extraction: "API design" instead of just "API"
- Multi-topic detection: "NBA and FIFA" → `["NBA", "FIFA"]`
- Nuanced classification: "Can you tell me everything about the database migration timeline?" → correctly identified as needing both cluster and atomic data
- Temporal intent detection: "What happened yesterday" → `temporal="recent"`

**Cost:** ~$0.001 per query using `gemini-2.5-flash-lite` (the model is already configured but unused)

**Estimated effort:** Medium

### Solution F: Memory Quality Pipeline

- [ ] **Approved** | - [ ] **In Progress** | - [ ] **Done**

**Addresses: #1.13**

The eval plan documents quality at 5.25/10. Improvements:

**F1: Extraction-time quality filter**
```python
def is_quality_memory(fact: str) -> bool:
    """Reject low-quality extractions before storing."""
    # Too short to be useful
    if len(fact) < 40:
        return False

    # Vague/context-dependent patterns
    vague = ["the user", "the process", "this was", "it was",
             "the output", "the same", "as mentioned", "was adjusted"]
    if any(p in fact.lower() for p in vague):
        return False

    # Must contain at least one specific noun/entity
    # (simple heuristic: has a capitalized word that isn't sentence-start)
    words = fact.split()
    has_entity = any(w[0].isupper() for w in words[1:] if len(w) > 1)
    has_number = any(c.isdigit() for c in fact)
    if not has_entity and not has_number:
        # Likely too generic
        return False

    return True
```

**F2: Reduce facts-per-message target**
Adjust the extraction prompt to request 1-2 high-quality facts instead of extracting everything:
```
Extract only the MOST IMPORTANT 1-2 facts from this message.
Each fact MUST be self-contained — understandable without reading the original message.
Do NOT extract obvious, trivial, or context-dependent statements.
```

**F3: Post-extraction quality scoring**
Score memories at insertion time and store the score. Use it as a retrieval-time boost:
```python
# In hybrid_search, adjust score by quality
for mem in results:
    quality = mem.get("quality_score", 0.5)
    mem["score"] = mem["score"] * (0.7 + 0.3 * quality)  # Quality-weighted
```

**Estimated effort:** Medium

### Solution G: Adaptive Hybrid Alpha Per Query Type

- [ ] **Approved** | - [ ] **In Progress** | - [ ] **Done**

**Addresses: #1.14**

Wire `get_adaptive_alpha()` into the hierarchical retrieval service:

```python
# In _retrieve_clusters and _retrieve_atomic:
async def _retrieve_clusters(self, channel_id, query, topic_filter, limit):
    return await hybrid_search(
        channel_id=channel_id,
        query=query,
        tier_filter=MemoryTier.TOPIC_CLUSTER.value,
        topic_filter=topic_filter,
        limit=limit,
        alpha=None,  # Let hybrid_search use get_adaptive_alpha()
    )
```

By passing `alpha=None` instead of `settings.hybrid_alpha`, the existing `get_adaptive_alpha()` function will be used automatically. This is a one-line change per retrieval method.

**Estimated effort:** Very low

### Solution H: Cross-Tier Semantic Deduplication

- [ ] **Approved** | - [ ] **In Progress** | - [ ] **Done**

**Addresses: #1.15**

After combining results from multiple tiers, perform semantic dedup:

```python
def _semantic_dedup(self, memories: list[dict], threshold: float = 0.85) -> list[dict]:
    """Remove semantically similar memories across tiers, preferring more specific ones."""
    unique = []
    for mem in memories:
        is_dup = False
        for existing in unique:
            # Simple text similarity via word overlap (no embedding needed)
            sim = self._jaccard_similarity(
                mem.get("memory", "").lower().split(),
                existing.get("memory", "").lower().split()
            )
            if sim > threshold:
                # Keep the more specific one (longer text, or lower tier)
                if len(mem.get("memory", "")) > len(existing.get("memory", "")):
                    unique.remove(existing)
                    unique.append(mem)
                is_dup = True
                break
        if not is_dup:
            unique.append(mem)
    return unique
```

**Estimated effort:** Low

---

## 3. Implementation Roadmap

### Phase 1: Quick Wins (1-2 days)

- [ ] **Started** | - [ ] **Completed**

These require minimal code changes and have immediate impact:

| # | Solution | Effort | Impact | Addresses |
|---|----------|--------|--------|-----------|
| 1 | **D: Apply temporal decay** — add 1 line to `retrieve()` | Very low | High | #1.4 |
| 2 | **G: Adaptive alpha** — pass `alpha=None` in 3 methods | Very low | Medium | #1.14 |
| 3 | **C: Score-based expansion** — replace count checks with score checks | Low | High | #1.2 |

### Phase 2: Consolidation Fix (2-3 days)

- [ ] **Started** | - [ ] **Completed**

This is the prerequisite for all cluster-based improvements:

| # | Solution | Effort | Impact | Addresses |
|---|----------|--------|--------|-----------|
| 4 | **Fix `_link_memories_to_cluster()`** — implement Weaviate property update | Low | Critical | #1.11 |
| 5 | **Clean up existing duplicate clusters** — one-time migration | Low | High | #1.11 |
| 6 | **Add `cluster_id` filter to `hybrid_search()`** | Low | Medium | Prereq for A |

### Phase 3: Retrieval Redesign (1-2 weeks)

- [ ] **Started** | - [ ] **Completed**

With consolidation fixed, implement the core retrieval improvements:

| # | Solution | Effort | Impact | Addresses |
|---|----------|--------|--------|-----------|
| 7 | **A: Topic-first retrieval** — two-stage coarse-to-fine search | Medium | High | #1.1, #1.3, #1.11 |
| 8 | **B: Bidirectional expansion** — add upward expansion path | Medium | Medium | #1.1, #1.3 |
| 9 | **E: LLM query classification** — hybrid regex + flash-lite | Medium | High | #1.10 |
| 10 | **H: Semantic dedup** — cross-tier similarity check | Low | Low | #1.15 |

### Phase 4: Quality & Ecosystem (2-4 weeks)

- [ ] **Started** | - [ ] **Completed**

Longer-term improvements for overall system quality:

| # | Solution | Effort | Impact | Addresses |
|---|----------|--------|--------|-----------|
| 11 | **F: Memory quality pipeline** — extraction filter + scoring | Medium | High | #1.13 |
| 12 | **Feedback loop** — track citation usage + user ratings | High | High | #1.5 |
| 13 | **Cross-channel search** — multi-channel query routing | Medium | Medium | #1.12 |
| 14 | **Real-time sync** — Socket Mode integration | High | Medium | #1.7 |
| 15 | **Memory TTL/pruning** — automated expiration | Medium | Medium | #1.8 |

### Dependency Graph

```
Phase 1 (Quick Wins) ──────────────────────────────────────────────┐
  D: Temporal decay ─────────────────────── standalone             │
  G: Adaptive alpha ─────────────────────── standalone             │
  C: Score-based expansion ──────────────── standalone             │
                                                                   │
Phase 2 (Consolidation Fix) ──────────────────────────────────┐    │
  Fix _link_memories_to_cluster ──┬── prerequisite for ───┐   │    │
  Clean up duplicate clusters ────┘                       │   │    │
  Add cluster_id filter ──────────────────────────────────│───┘    │
                                                          │        │
Phase 3 (Retrieval Redesign) ─────────────────────────────│────────┘
  A: Topic-first retrieval ◄──────────────────────────────┘
  B: Bidirectional expansion ──── standalone (enhanced by A)
  E: LLM query classification ── standalone (enhanced by A)
  H: Semantic dedup ──────────── standalone

Phase 4 (Quality & Ecosystem) ── all standalone
  F: Memory quality pipeline
  Feedback loop
  Cross-channel search
  Real-time sync
  Memory TTL/pruning
```

### Expected Cumulative Impact

| After Phase | Retrieval Precision | Key Capability Gained |
|-------------|--------------------|-----------------------|
| Phase 1 | +15-20% | Recent results rank higher; better alpha tuning; smarter expansion |
| Phase 2 | +5% (indirect) | Unlocks cluster-based retrieval; stops duplicate cluster growth |
| Phase 3 | +30-40% | Topic-scoped search; multi-topic queries; bidirectional expansion |
| Phase 4 | +10-15% | Cleaner index; cross-channel; real-time data |

---

*This document captures improvement ideas based on validated codebase analysis. Precision estimates are directional, not measured. Actual impact should be validated using the evaluation framework proposed in `docs/architecture/09-MEMORY_EVAL_PLAN.md`.*
