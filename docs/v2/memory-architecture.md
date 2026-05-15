# Beever Atlas — Memory Architecture Reference

This document describes the 3-tier memory system and graph knowledge layer. Use it to understand the data structures available when building features that consume memory (wiki generation, QA agent, search, etc.).

---

## Architecture Overview

```
Raw Messages (Slack/Discord/Teams)
    │
    ▼
┌─────────────────────────────────────────────────────┐
│  INGESTION PIPELINE (per batch of messages)          │
│  Preprocessor → Fact Extractor → Entity Extractor    │
│  → Embedder → Cross-Batch Validator → Persister      │
└─────────────────────────────────────────────────────┘
    │                          │
    ▼                          ▼
┌──────────────┐      ┌──────────────────┐
│  Weaviate     │      │  Neo4j            │
│  (3-tier      │      │  (knowledge       │
│   memory)     │      │   graph)          │
└──────────────┘      └──────────────────┘
    │                          │
    ▼                          ▼
┌─────────────────────────────────────────────────────┐
│  CONSOLIDATION PIPELINE (after sync completes)       │
│  Clustering → Context Building → LLM Summaries       │
│  → Graph Enrichment → Cross-Cluster Links            │
└─────────────────────────────────────────────────────┘
    │
    ▼
  Tier 1 (TopicCluster) + Tier 0 (ChannelSummary)
```

---

## Tier 2 — Atomic Facts (Weaviate)

**What**: Individual extracted facts from messages. The retrieval unit for QA search.

**Model**: `AtomicFact` (`src/beever_atlas/models/domain.py`)

**Key fields**:

| Field | Type | Description |
|-------|------|-------------|
| `id` | str | Deterministic UUID from `platform:channel_id:message_ts:fact_index` |
| `memory_text` | str | Self-contained fact (1-2 sentences). Includes rationale and context — e.g. "Alice decided to use Redis for session caching after evaluating Memcached, citing pub/sub support." |
| `quality_score` | float | 0.0–1.0 composite of specificity, actionability, verifiability |
| `fact_type` | str | `"decision"` / `"opinion"` / `"observation"` / `"action_item"` / `"question"` |
| `importance` | str | `"low"` / `"medium"` / `"high"` / `"critical"` |
| `topic_tags` | list[str] | 1-3 thematic labels (e.g. "deployment", "auth") |
| `entity_tags` | list[str] | Named entities mentioned (people, projects, tools) |
| `action_tags` | list[str] | Action verbs (e.g. "decided", "blocked", "shipped") |
| `author_name` | str | Display name of message author |
| `message_ts` | str | Timestamp of source message |
| `thread_context_summary` | str | 1-sentence deliberation arc for threaded discussions |
| `cluster_id` | str | Which TopicCluster this fact belongs to |
| `superseded_by` | str | ID of newer fact that replaces this one (null if current) |
| `supersedes` | str | ID of older fact this one replaces |
| `source_media_urls` | list[str] | URLs of attached media (images, PDFs, videos) |
| `source_media_type` | str | `"image"` / `"pdf"` / `"video"` / `"audio"` / `""` |
| `source_link_urls` | list[str] | URLs shared in the message |
| `source_link_titles` | list[str] | Titles of shared links |
| `text_vector` | list[float] | Jina v4 embedding (2048-dim) for semantic search |

**API**: `GET /api/channels/{channel_id}/memories?page=1&limit=50&topic=&entity=&importance=`

**How to query**: Weaviate hybrid search (keyword + semantic) using `text_vector`. Filter by `channel_id`, `topic_tags`, `entity_tags`, `importance`, `fact_type`, timestamp range.

---

## Tier 1 — Topic Clusters (Weaviate)

**What**: Semantic groupings of related atomic facts. Each cluster represents a knowledge area with multi-angle summaries and structured enrichment.

**Model**: `TopicCluster` (`src/beever_atlas/models/domain.py`)

**Key fields**:

| Field | Type | Description |
|-------|------|-------------|
| `id` | str | UUID |
| `title` | str | Short descriptive name (5-10 words, e.g. "JWT Migration to RS256") |
| `summary` | str | Narrative of what happened (2-3 sentences) |
| `current_state` | str | Where things stand now (1-2 sentences) |
| `open_questions` | str | Unresolved tensions/debates (1-2 sentences, empty if resolved) |
| `impact_note` | str | Scope and significance (1 sentence) |
| `topic_tags` | list[str] | 3 most representative tags (LLM-selected, not merged from all members) |
| `member_ids` | list[str] | IDs of member AtomicFacts |
| `member_count` | int | Number of member facts |
| `status` | str | `"active"` / `"completed"` / `"stale"` |
| `staleness_score` | float | 0.0 (fresh) to 1.0 (very stale) |
| `key_facts` | list[dict] | Top 5 facts by quality_score with attribution: `{fact_id, memory_text, author_name, message_ts, fact_type, importance, quality_score, source_message_id}` |
| `decisions` | list[dict] | Decisions with supersede chains: `{name, decided_by, status, superseded_by, date, context}` |
| `people` | list[dict] | Contributors with roles: `{name, role, entity_id}`. Roles: `decision_maker` / `expert` / `contributor` / `mentioned` |
| `technologies` | list[dict] | Tech mentioned: `{name, category, champion}` |
| `projects` | list[dict] | Projects: `{name, status, owner, blockers}` |
| `faq_candidates` | list[dict] | Q&A pairs: `{question, answer}` |
| `key_entities` | list[dict] | Graph entities: `{id, name, type}` |
| `key_relationships` | list[dict] | Graph relationships: `{source, type, target, confidence}` |
| `authors` | list[str] | All contributor names |
| `date_range_start/end` | str | Temporal span of member facts |
| `media_refs` | list[str] | Media URLs from member facts |
| `link_refs` | list[str] | Link URLs from member facts |
| `fact_type_counts` | dict | `{"decision": N, "question": N, ...}` |
| `related_cluster_ids` | list[str] | IDs of clusters sharing 2+ entity tags |
| `centroid_vector` | list[float] | Mean embedding of all member facts |

**API**: `GET /api/channels/{channel_id}/topics` (sorted by member_count desc)

**Clustering**: Embedding-based cosine similarity against cluster centroids (threshold 0.7). No LLM involved in clustering — only in summary generation.

---

## Tier 0 — Channel Summary (Weaviate)

**What**: High-level channel overview synthesizing all topic clusters. One per channel.

**Model**: `ChannelSummary` (`src/beever_atlas/models/domain.py`)

**Key fields**:

| Field | Type | Description |
|-------|------|-------------|
| `channel_id` | str | Channel identifier |
| `channel_name` | str | Resolved display name (e.g. "#backend-engineering") |
| `text` | str | Overall narrative (3-5 sentences) |
| `description` | str | One-line channel purpose (max 200 chars) |
| `themes` | str | How knowledge areas interrelate (2-3 sentences) |
| `momentum` | str | What's active/completed/stale, velocity (1-2 sentences) |
| `team_dynamics` | str | Who drives decisions, collaboration patterns (1-2 sentences) |
| `cluster_count` | int | Number of topic clusters |
| `fact_count` | int | Total facts across all clusters |
| `top_decisions` | list[dict] | Channel-wide decisions: `{name, decided_by, status, superseded_by, date, topic_cluster_id, context}` |
| `top_people` | list[dict] | Contributors aggregated: `{name, role, topic_count, expertise_topics}` (highest role wins across clusters) |
| `tech_stack` | list[dict] | Technologies: `{name, category, champion, topic_count}` |
| `active_projects` | list[dict] | Projects: `{name, status, owner, blockers, topic_cluster_id}` |
| `glossary_terms` | list[dict] | Channel jargon: `{term, definition, first_mentioned_by, related_topics}` |
| `recent_activity_summary` | dict | Last 7 days: `{facts_added_7d, decisions_added_7d, new_topics, updated_topics, highlights}` |
| `topic_graph_edges` | list[dict] | Edges between topics: `{source_cluster_id, target_cluster_id, source_title, target_title, shared_entities}` |
| `key_topics` | list[dict] | All topics: `{tags, title, member_count, status}` |
| `worst_staleness` | float | Max staleness across all clusters |

**API**: `GET /api/channels/{channel_id}/summary`

---

## Graph Memory (Neo4j)

**What**: Knowledge graph of entities and relationships extracted from messages. Complements the vector memory with structured, traversable connections.

**Protocol**: `GraphStore` (`src/beever_atlas/stores/graph_protocol.py`)

### Entity Types

| Type | Scope | Examples |
|------|-------|---------|
| `Person` | global | Alice, Bob, Charlie |
| `Technology` | global | Redis, Neo4j, Kubernetes |
| `Project` | global | Atlas, Auth Migration |
| `Team` | global | Backend Team, Mobile Team |
| `Decision` | channel | "Use RS256 for JWT signing" |
| `Meeting` | channel | "Sprint Review March 20" |
| `Artifact` | channel | "API Spec v3", "Architecture Diagram" |

**Entity fields**: `name`, `type`, `scope`, `properties` (role, category, status, etc.), `aliases`, `status` (active/pending)

### Relationship Types

| Relationship | Meaning | Example |
|-------------|---------|---------|
| `DECIDED` | Person made a decision | Alice → DECIDED → Use RS256 |
| `WORKS_ON` | Person works on project | Bob → WORKS_ON → Atlas |
| `USES` | Person/project uses tech | Atlas → USES → Redis |
| `OWNS` | Person owns project | Alice → OWNS → Auth Module |
| `BLOCKED_BY` | Project blocked by another | Rate Limiting → BLOCKED_BY → Redis Upgrade |
| `SUPERSEDES` | Decision replaces another | Use RS256 → SUPERSEDES → Use HS256 |
| `DEPENDS_ON` | Project depends on another | API v2 → DEPENDS_ON → Auth Migration |
| `REPORTS_TO` | Person reports to person | Bob → REPORTS_TO → Alice |
| `MENTIONED_IN` | Entity mentioned in fact | Redis → MENTIONED_IN → Event(fact_id) |

**Relationship fields**: `type`, `source`, `target`, `confidence` (0.0-1.0), `valid_from`, `context`

### Episodic Links

Entities are connected to facts via `MENTIONED_IN` edges to `Event` nodes. Each Event stores `weaviate_fact_id`, `message_ts`, `channel_id`, `media_urls`, `link_urls`.

### Key Query Patterns

```python
# Get all decisions for a channel
decisions = await graph.get_decisions(channel_id, limit=20)

# Get entity neighborhood (1-2 hops)
subgraph = await graph.get_neighbors(entity_id, hops=2, limit=50)

# List entities by type
people = await graph.list_entities(channel_id, entity_type="Person", limit=100)

# List relationships
rels = await graph.list_relationships(channel_id, limit=200)

# Find entity by name
entity = await graph.find_entity_by_name("Redis")
```

---

## How Data Flows for Consumers

### For Wiki Generation

The wiki builder should read pre-computed structured data — no additional LLM calls needed:

```
ChannelSummary → Overview section (text, description)
               → Themes section (themes)
               → Momentum section (momentum, recent_activity_summary)
               → People section (top_people, team_dynamics)
               → Tech Stack section (tech_stack)
               → Projects section (active_projects)
               → Decisions section (top_decisions with supersede chains)
               → Glossary section (glossary_terms)
               → Topic graph (topic_graph_edges → mermaid diagram)

TopicCluster[] → Topic pages (title, summary, current_state, open_questions)
               → Key facts per topic (key_facts with citation)
               → Decisions per topic (decisions)
               → People per topic (people with roles)
               → FAQ section (faq_candidates)

AtomicFact[]   → Source citations ([1] @author · date · View)
               → Media & Resources section (source_media_urls, source_link_urls)
```

### For QA Agent

The QA agent should use a hybrid retrieval strategy:

1. **Vector search** (Weaviate) — query `text_vector` on AtomicFacts for semantic match
2. **Keyword filter** — filter by `topic_tags`, `entity_tags`, `fact_type`, `importance`
3. **Graph traversal** (Neo4j) — expand entity neighborhoods for related context
4. **Tier routing** — broad questions → Tier 0/1 summaries; specific questions → Tier 2 facts

```
User question
    │
    ├─ "What's this channel about?" → ChannelSummary.text + description
    ├─ "What did we decide about auth?" → TopicCluster(topic_tags∋"auth").decisions + key_facts
    ├─ "Who works on Redis?" → graph.get_neighbors("Redis") → Person entities
    └─ "When did we switch to RS256?" → AtomicFact search(fact_type="decision", entity_tags∋"RS256")
```

---

## File Map

| File | What |
|------|------|
| `src/beever_atlas/models/domain.py` | Domain models: AtomicFact, TopicCluster, ChannelSummary, GraphEntity, GraphRelationship |
| `src/beever_atlas/services/consolidation.py` | Consolidation pipeline: clustering, context building, LLM summaries, graph enrichment |
| `src/beever_atlas/agents/schemas/consolidation.py` | LLM output schemas: TopicSummaryResult, ChannelSummaryResult, FaqCandidate, GlossaryTerm |
| `src/beever_atlas/agents/consolidation/summarizer.py` | Summarizer agent factories (topic + channel) |
| `src/beever_atlas/agents/prompts/fact_extractor.py` | Fact extraction prompt with type-specific context guidance |
| `src/beever_atlas/stores/weaviate_store.py` | Weaviate CRUD for all tiers |
| `src/beever_atlas/stores/graph_protocol.py` | GraphStore protocol (Neo4j/NebulaGraph) |
| `src/beever_atlas/stores/neo4j_store.py` | Neo4j implementation of GraphStore |
| `src/beever_atlas/api/topics.py` | REST API for topics, summaries, entity cards |
| `src/beever_atlas/services/batch_processor.py` | Batch processing with checkpoint/resume |
| `src/beever_atlas/services/sync_runner.py` | Sync orchestration (fetch → process → consolidate) |
