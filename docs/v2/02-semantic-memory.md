# Semantic Memory: Weaviate 3-Tier Design

## Context

Beever Atlas uses a dual-memory architecture. This document specifies the **semantic memory layer** (Weaviate), which handles approximately **80% of all queries** — factual lookups, topical questions, and multimodal content retrieval. The other 20% of queries (relational, temporal, multi-hop) are handled by the graph memory layer; see [`03-graph-memory.md`](./03-graph-memory.md).

These two stores are complementary, not redundant. Weaviate cannot perform multi-hop graph traversal ("who decided X, and what blocked them?"), and Neo4j cannot do fuzzy BM25+vector hybrid ranking across tens of thousands of atomic facts. For how data enters both stores simultaneously during ingestion, see [`05-ingestion-pipeline.md`](./05-ingestion-pipeline.md).

---

## 3.2 Semantic Memory: Weaviate (3-Tier, Improved)

The v1 hierarchical design was sound — the implementation was broken. v2 keeps the 3-tier architecture but fixes every weakness.

```
┌─────────────────────────────────────────────────────────────────────┐
│              SEMANTIC MEMORY: WEAVIATE (3-Tier)                      │
│                                                                      │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  TIER 0: Channel Summary                                      │  │
│  │  • Channel-level overview ("what's happening?")               │  │
│  │  • Updated by consolidation service                           │  │
│  │  • Used for wiki overview section                             │  │
│  │  • Query: "Catch me up", "Overview", "Status update"          │  │
│  │  • Access: FREE (cached, no LLM needed)                       │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              │                                       │
│                    consolidates from                                  │
│                              ▼                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  TIER 1: Topic Clusters                                       │  │
│  │  • Grouped memories by topic (authentication, deployment...)  │  │
│  │  • Each cluster has: summary, member_ids, topic_tags          │  │
│  │  • member_ids ACTUALLY LINKED to Tier 2 atomics (v1 fix!)    │  │
│  │  • Used for topic-level questions and wiki topic sections     │  │
│  │  • Query: "Tell me about auth", "What about deployment?"     │  │
│  │  • Access: FREE (cached, no LLM needed)                       │  │
│  │                                                                │  │
│  │  v2 FIXES:                                                     │  │
│  │  ✓ _link_memories_to_cluster() actually writes cluster_id    │  │
│  │  ✓ MERGE-based dedup prevents duplicate clusters              │  │
│  │  ✓ Two-stage topic-first retrieval (coarse → fine)           │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                              │                                       │
│                    consolidates from                                  │
│                              ▼                                       │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  TIER 2: Atomic Facts                                         │  │
│  │  • Individual facts with full metadata and citations          │  │
│  │  • Named vectors: text (2048-dim), image, doc (Jina v4)      │  │
│  │  • Cross-modal search (text query → find images/PDFs)         │  │
│  │  • Quality-scored at extraction (v2: reject < 0.5)            │  │
│  │  • Linked to Neo4j via graph_entity_ids                       │  │
│  │  • Query: "What exactly did Alice say?", "Find the diagram"  │  │
│  │  • Access: PAID (uses embedding for search)                   │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  Wiki-First Cost Optimization (preserved from v1):                  │
│  • Tier 0 + Tier 1 reads = FREE (pre-generated, cached)            │
│  • Tier 2 search = CHEAP (embedding only, ~$0.001)                  │
│  • LLM synthesis = PAID (only when needed, ~$0.02)                  │
│  • Average query cost: ~$0.01 (5x cheaper than competitors)         │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Weaviate Schema

```python
properties = [
    # === Core ===
    Property(name="memory", data_type=DataType.TEXT),
    Property(name="channel_id", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="source", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="platform", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="timestamp", data_type=DataType.NUMBER),

    # === Hierarchy (FIXED in v2) ===
    Property(name="tier", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="cluster_id", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="member_ids", data_type=DataType.TEXT_ARRAY, skip_vectorization=True),
    Property(name="member_count", data_type=DataType.INT),

    # === Graph Linkage (NEW) ===
    Property(name="graph_entity_ids", data_type=DataType.TEXT_ARRAY,
             description="Neo4j node IDs extracted from this memory"),

    # === Quality (NEW) ===
    Property(name="quality_score", data_type=DataType.NUMBER),

    # === Temporal (NEW) ===
    Property(name="valid_at", data_type=DataType.DATE),
    Property(name="invalid_at", data_type=DataType.DATE),

    # === Tagging ===
    Property(name="topic_tags", data_type=DataType.TEXT_ARRAY, skip_vectorization=True),
    Property(name="entity_tags", data_type=DataType.TEXT_ARRAY, skip_vectorization=True),
    Property(name="action_tags", data_type=DataType.TEXT_ARRAY, skip_vectorization=True),
    Property(name="importance", data_type=DataType.TEXT, skip_vectorization=True),

    # === Citations ===
    Property(name="message_ts", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="thread_ts", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="user_name", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="slack_user_id", data_type=DataType.TEXT, skip_vectorization=True),

    # === Files ===
    Property(name="file_id", data_type=DataType.TEXT, skip_vectorization=True),
    Property(name="filename", data_type=DataType.TEXT, skip_vectorization=True),
]
# Named vectors: text_vector, image_vector, doc_vector (2048-dim Jina v4)
```

---

## Retrieval Improvements (All 15 Weaknesses Fixed)

> **ADK Implementation:** The retrieval methods below are exposed as ADK `FunctionTool` instances on the `semantic_agent`: `search_weaviate_hybrid`, `get_tier0_summary`, `get_tier1_clusters`. The agent orchestrates the multi-step retrieval flow (coarse-to-fine, expansion, decay, dedup). See [`13-adk-integration.md`](13-adk-integration.md).

```python
class ImprovedSemanticRetriever:
    """Weaviate retrieval with all v1 weaknesses fixed."""

    async def retrieve(self, query: str, channel_id: str | None,
                       query_understanding: QueryUnderstanding) -> list[dict]:

        depth = query_understanding.semantic_depth  # "overview", "topic", "detail", "auto"

        if depth == "overview":
            # Tier 0 → optional expand to Tier 1
            memories = await self._retrieve_summary(channel_id, query)
            if self._should_expand(memories, "down"):
                memories += await self._retrieve_clusters(channel_id, query)

        elif depth == "topic":
            # FIX 1.3 + 1.11: Two-stage topic-first retrieval
            # Stage 1 (coarse): Find relevant topic clusters
            clusters = await self._retrieve_clusters(
                channel_id, query,
                topic_filter=query_understanding.topics,
                alpha=None,  # FIX 1.14: Adaptive alpha
            )
            # Stage 2 (fine): Search atomics WITHIN matched clusters
            if clusters:
                member_ids = self._collect_member_ids(clusters)
                atomics = await self._retrieve_atomics_scoped(
                    channel_id, query, member_ids,
                    alpha=None,  # FIX 1.14
                )
                memories = clusters + atomics
            else:
                # No matching clusters → fall back to global atomic search
                memories = await self._retrieve_atomics(channel_id, query)

            # FIX 1.1: Bidirectional — expand UP if results are weak
            if self._should_expand(memories, "up"):
                summaries = await self._retrieve_summary(channel_id, query)
                memories = self._merge_and_rerank(memories, summaries)

        else:  # detail
            # Direct atomic search, with optional upward expansion
            memories = await self._retrieve_atomics(
                channel_id, query, alpha=None,  # FIX 1.14
            )
            # FIX 1.1: Can expand UP to clusters for broader context
            if self._should_expand(memories, "up"):
                clusters = await self._retrieve_clusters(channel_id, query)
                memories = self._merge_and_rerank(memories, clusters)

        # FIX 1.4: Apply temporal decay to ranking
        self._apply_temporal_decay(memories)

        # FIX 1.13: Quality-weighted ranking boost
        self._apply_quality_boost(memories)

        # FIX 1.15: Semantic dedup across tiers
        memories = self._semantic_dedup(memories)

        return memories[:max_results]

    def _should_expand(self, memories: list, direction: str) -> bool:
        """FIX 1.2: Score-based expansion, not count-based."""
        if not memories:
            return True
        scores = [m.get("score", 0) for m in memories]
        return max(scores) < 0.6 or (sum(scores) / len(scores)) < 0.4

    def _apply_temporal_decay(self, memories: list) -> None:
        """FIX 1.4: Actually apply the existing temporal decay function."""
        for m in memories:
            days_ago = self._days_since(m.get("timestamp"))
            m["score"] = self.temporal_decay.apply(m["score"], days_ago, m)
        memories.sort(key=lambda m: m.get("score", 0), reverse=True)

    def _apply_quality_boost(self, memories: list) -> None:
        """FIX 1.13: Quality-weighted ranking — good memories score higher."""
        for m in memories:
            quality = m.get("quality_score", 0.5)
            m["score"] = m["score"] * (0.7 + 0.3 * quality)
        memories.sort(key=lambda m: m.get("score", 0), reverse=True)

    def _semantic_dedup(self, memories: list, threshold=0.85) -> list:
        """FIX 1.15: Remove near-duplicates across tiers."""
        unique = []
        for mem in memories:
            is_dup = any(
                self._jaccard_similarity(mem["memory"], e["memory"]) > threshold
                for e in unique
            )
            if not is_dup:
                unique.append(mem)
        return unique
```

---

## 4.3 Temporal Decay Configuration

```python
class TemporalDecay:
    """Ebbinghaus-based temporal decay with exemptions and reinforcement."""
    DEFAULT_DECAY_RATE = 0.1

    # Facts with these action_tags decay at half rate
    SLOW_DECAY_TAGS = {"decision", "architecture", "policy", "deadline"}

    # Facts with these importance levels are exempt from decay
    EXEMPT_IMPORTANCE = {"high", "critical"}

    def apply(self, score: float, days_ago: float, fact: dict) -> float:
        """Apply temporal decay to a retrieval score."""
        if fact.get("importance") in self.EXEMPT_IMPORTANCE:
            return score  # No decay for high-importance facts

        rate = self.DEFAULT_DECAY_RATE
        # Half decay for architectural decisions
        if any(tag in self.SLOW_DECAY_TAGS for tag in fact.get("action_tags", [])):
            rate *= 0.5

        # Citation reinforcement: cited facts decay slower
        citation_count = fact.get("citation_count", 0)
        if citation_count > 0:
            rate = rate / (1 + 0.1 * citation_count)

        decay = math.exp(-rate * (days_ago / 30))
        return score * decay
```

**Decay behavior at `DECAY_RATE = 0.1`:**

| Fact Age | Score Multiplier | Effect |
|----------|-----------------|--------|
| 1 day | 0.997 | Essentially no decay |
| 7 days | 0.977 | Minimal (~2% reduction) |
| 30 days | 0.905 | Mild (~10% reduction) |
| 90 days | 0.741 | Moderate (~26% reduction) |
| 180 days | 0.549 | Significant (~45% reduction) |
| 365 days | 0.295 | Strong (~70% reduction) |

**Exemptions:**
- Facts tagged `importance: "high"` or `"critical"` → no decay
- Facts tagged `action_tags: ["decision", "architecture", "policy"]` → half decay rate (0.05)
- Facts cited 5+ times → effective rate drops to ~0.067

**Configuration:**
```python
# In config.py Settings
decay_rate: float = 0.1
decay_slow_tags: list[str] = ["decision", "architecture", "policy", "deadline"]
decay_exempt_importance: list[str] = ["high", "critical"]
decay_reinforcement_factor: float = 0.1
```
