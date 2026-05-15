# Graph Memory: Neo4j Flexible Knowledge Graph

## Context

Beever Atlas uses a dual-memory architecture. This document specifies the **graph memory layer** (Neo4j), which handles approximately **20% of all queries** — relational questions, temporal evolution tracking, and multi-hop traversal. The other 80% of queries (factual, topical, multimodal) are handled by the semantic memory layer; see [`02-semantic-memory.md`](./02-semantic-memory.md).

Neo4j handles what Weaviate fundamentally cannot: multi-hop traversal ("Person → works on → Project → has decision → blocked by → Constraint"), temporal chains ("how did this decision evolve?"), and precision relational lookups ("who owns this project?"). Graph results are routinely enriched by following episodic edges back to Weaviate to retrieve the original fact text and Slack citations. For how data enters both stores simultaneously during ingestion, see [`05-ingestion-pipeline.md`](./05-ingestion-pipeline.md).

---

## 3.3 Graph Memory: Neo4j (Flexible)

The graph memory captures **relationship meaning** from conversations — things that semantic search fundamentally cannot handle.

```
┌─────────────────────────────────────────────────────────────────────┐
│                GRAPH MEMORY: Neo4j (Flexible)                        │
│                                                                      │
│  PURPOSE: Capture WHO did WHAT, WHEN, and HOW things RELATE         │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  GUIDED-FLEXIBLE ENTITY SCHEMA                                 │ │
│  │                                                                │ │
│  │  All nodes share a base:                                       │ │
│  │  ┌──────────────────────────────────┐                         │ │
│  │  │  name:        str    (required)  │                         │ │
│  │  │  entity_type: str    (required)  │                         │ │
│  │  │  description: str    (optional)  │                         │ │
│  │  │  channel:     str               │                         │ │
│  │  │  platform:    str               │                         │ │
│  │  │  properties:  dict   (flexible) │                         │ │
│  │  │  created_at:  datetime          │                         │ │
│  │  │  updated_at:  datetime          │                         │ │
│  │  └──────────────────────────────────┘                         │ │
│  │                                                                │ │
│  │  Core types (LLM prefers these):                              │ │
│  │  Person, Decision, Project, Technology                        │ │
│  │                                                                │ │
│  │  Extension types (LLM creates as needed):                     │ │
│  │  Team, Meeting, Artifact, Constraint, Budget, Deadline, ...   │ │
│  │                                                                │ │
│  │  Event node (episodic anchor):                                │ │
│  │  ┌──────────────────────────────────┐                         │ │
│  │  │  weaviate_id: str  → links to   │                         │ │
│  │  │               Weaviate atomic    │                         │ │
│  │  │  timestamp:   datetime           │                         │ │
│  │  │  channel:     str               │                         │ │
│  │  └──────────────────────────────────┘                         │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐ │
│  │  FLEXIBLE RELATIONSHIPS                                        │ │
│  │                                                                │ │
│  │  NOT a fixed list — LLM extracts whatever relationship        │ │
│  │  best captures the meaning:                                    │ │
│  │                                                                │ │
│  │  Common patterns:                                              │ │
│  │  Person  ──DECIDED──▶       Decision                          │ │
│  │  Person  ──WORKS_ON──▶      Project                           │ │
│  │  Person  ──MEMBER_OF──▶     Team                              │ │
│  │  Decision──AFFECTS──▶       Project                           │ │
│  │  Decision──SUPERSEDES──▶    Decision  (temporal evolution)    │ │
│  │  Decision──BLOCKED_BY──▶    Constraint                        │ │
│  │  Decision──USES──▶          Technology                        │ │
│  │  Project ──DEPENDS_ON──▶    Project                           │ │
│  │  Meeting ──PRODUCED──▶      Decision                          │ │
│  │  Any     ──MENTIONED_IN──▶  Event     (episodic link)        │ │
│  │  Any     ──ALIAS_OF──▶     Any       (entity dedup)          │ │
│  │                                                                │ │
│  │  Bidirectional edges (auto-created during ingestion):         │ │
│  │  DECIDED ↔ DECIDED_BY, BLOCKED_BY ↔ BLOCKS,                  │ │
│  │  WORKS_ON ↔ HAS_MEMBER, OWNS ↔ OWNED_BY                      │ │
│  │                                                                │ │
│  │  LLM can create ANY relationship type. The graph adapts       │ │
│  │  to whatever patterns exist in the organization's             │ │
│  │  conversations.                                                │ │
│  │                                                                │ │
│  │  Temporal properties on ALL relationships:                    │ │
│  │  • valid_from:  datetime                                      │ │
│  │  • valid_until: datetime (null = currently valid)             │ │
│  │  • created_at:  datetime (bi-temporal tracking)               │ │
│  │  • confidence:  float                                         │ │
│  └────────────────────────────────────────────────────────────────┘ │
│                                                                      │
│  EPISODIC LINKING (graph ↔ Weaviate):                               │
│  • Every graph entity connects to Event nodes                       │
│  • Event.weaviate_id → points to atomic fact in Weaviate           │
│  • Enables: graph traversal → find entities → follow episodic      │
│    edges → retrieve original fact text + Slack citations            │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Entity Scoping Rules

Global entities (Person, Technology, Project, Team) are **MERGED by name only** — the same entity spans all channels. Channel-scoped entities (Decision, Meeting, Artifact) are **MERGED by name + channel**.

| Entity Type | Scope | Merge Key | Rationale |
|---|---|---|---|
| Person | global | name | Alice is Alice everywhere |
| Technology | global | name | React is React everywhere |
| Project | global | name | Project names span channels |
| Team | global | name | Teams span channels |
| Decision | channel | name + channel | Decisions are channel-contextual |
| Meeting | channel | name + channel | Meetings are channel-contextual |
| Artifact | channel | name + channel | Docs are channel-contextual |
| _(extension types)_ | channel | name + channel | Default for LLM-created types |

---

## Neo4j Implementation

```python
class Neo4jStore:
    """Flexible graph memory — any entity type, any relationship type.

    Entity scoping: Global entities (Person, Technology, Project, Team) are
    MERGED by name only — the same entity spans all channels. Channel-scoped
    entities (Decision, Meeting, Artifact) are MERGED by name + channel.
    """

    QUERY_TIMEOUT_MS = 5000  # Hard limit on all graph queries

    # Cross-channel scoping rules
    ENTITY_SCOPING = {
        "Person":     "global",    # Alice is Alice everywhere
        "Technology": "global",    # React is React everywhere
        "Project":    "global",    # Project names span channels
        "Team":       "global",    # Teams span channels
        "Decision":   "channel",   # Decisions are channel-contextual
        "Meeting":    "channel",   # Meetings are channel-contextual
        "Artifact":   "channel",   # Docs are channel-contextual
        # Extension types default to "channel"
    }

    async def upsert_entity(self, entity: dict) -> str:
        """Create/update entity with scope-aware MERGE."""
        entity_type = entity["type"]
        scope = self.ENTITY_SCOPING.get(entity_type, "channel")

        if scope == "global":
            # Global: MERGE on name only, track channels as array
            cypher = f"""
                MERGE (n:{entity_type} {{name: $name}})
                ON CREATE SET n += $props, n.created_at = datetime(),
                              n.channels = [$channel],
                              n.quality_score = $quality_score
                ON MATCH SET n += $props, n.updated_at = datetime(),
                             n.channels = CASE
                               WHEN NOT $channel IN n.channels
                               THEN n.channels + $channel
                               ELSE n.channels END,
                             n.quality_score = CASE
                               WHEN $quality_score > n.quality_score
                               THEN $quality_score ELSE n.quality_score END
                RETURN id(n) as node_id
            """
        else:
            # Channel-scoped: MERGE on name + channel
            cypher = f"""
                MERGE (n:{entity_type} {{name: $name, channel: $channel}})
                ON CREATE SET n += $props, n.created_at = datetime(),
                              n.quality_score = $quality_score
                ON MATCH SET n += $props, n.updated_at = datetime(),
                             n.quality_score = CASE
                               WHEN $quality_score > n.quality_score
                               THEN $quality_score ELSE n.quality_score END
                RETURN id(n) as node_id
            """
        return await self.execute(cypher,
            name=entity["name"], channel=entity.get("channel"),
            quality_score=entity.get("quality_score", 0.5),
            props={k: v for k, v in entity.get("properties", {}).items()
                   if v is not None},
        )

    async def upsert_relationship(self, rel: dict) -> None:
        """Create relationship with scope-aware matching + provenance."""
        rel_type = rel["type"]
        source_scope = self.ENTITY_SCOPING.get(rel.get("source_type"), "channel")
        target_scope = self.ENTITY_SCOPING.get(rel.get("target_type"), "channel")

        source_match = "{name: $source}" if source_scope == "global" \
                       else "{name: $source, channel: $channel}"
        target_match = "{name: $target}" if target_scope == "global" \
                       else "{name: $target, channel: $channel}"

        cypher = f"""
            MATCH (s {source_match})
            MATCH (t {target_match})
            MERGE (s)-[r:{rel_type}]->(t)
            SET r.context = $context,
                r.source_channel = $channel,
                r.valid_from = coalesce($valid_from, datetime()),
                r.created_at = datetime(),
                r.confidence = $confidence,
                r.evidence = $evidence,
                r.source_message_id = $source_message_id,
                r.source_fact_id = $source_fact_id,
                r.extracted_at = datetime()
        """
        await self.execute(cypher, **rel)

    async def create_episodic_link(self, entity_name: str, weaviate_id: str,
                                    channel: str, timestamp: float) -> None:
        """Link a graph entity to its source fact in Weaviate."""
        # Try global match first, then channel-scoped
        await self.execute("""
            MATCH (n)
            WHERE n.name = $name
              AND (n.channel = $channel OR $channel IN n.channels)
            MERGE (e:Event {weaviate_id: $wid})
            ON CREATE SET e.channel = $channel, e.timestamp = $ts
            MERGE (n)-[:MENTIONED_IN]->(e)
        """, name=entity_name, channel=channel, wid=weaviate_id, ts=timestamp)

    async def traverse(self, start_entities: list[str], channel: str = None,
                       max_hops: int = 2) -> list[dict]:
        """Bounded, directed traversal with APOC path expansion."""
        return await self.execute_with_timeout("""
            MATCH (start)
            WHERE start.name IN $entities
              AND ($channel IS NULL
                   OR start.channel = $channel
                   OR $channel IN start.channels)
            CALL apoc.path.expandConfig(start, {
                minLevel: 1,
                maxLevel: $max_hops,
                uniqueness: 'NODE_GLOBAL',
                limit: 50,
                relationshipFilter: '>'
            }) YIELD path
            WHERE all(r IN relationships(path) WHERE
                r.valid_until IS NULL OR r.valid_until > datetime())
            RETURN path
        """, entities=start_entities, channel=channel, max_hops=max_hops)

    async def temporal_chain(self, entity_name: str, channel: str = None) -> list[dict]:
        """Bounded SUPERSEDES chain (max 5 hops, distinct per level)."""
        return await self.execute_with_timeout("""
            MATCH (d:Decision)
            WHERE d.name CONTAINS $name
              AND ($channel IS NULL OR d.channel = $channel
                   OR $channel IN d.channels)
            MATCH path = (d)-[:SUPERSEDES*0..5]->(older:Decision)
            WITH DISTINCT older, path
            RETURN path ORDER BY older.valid_from DESC
            LIMIT 20
        """, name=entity_name, channel=channel)

    async def comprehensive_traverse(self, start_entities: list[str],
                                      channel: str = None,
                                      max_hops: int = 3,
                                      max_nodes: int = 200) -> dict:
        """Collect-all traversal: gather ALL relationships within N hops,
        then let the LLM analyze relevance. Inspired by Forensic Eyes'
        Phase 16 pattern — avoids brittleness from pre-filtering edge types.

        Use for complex graph queries where relationship types are diverse
        and pre-filtering risks missing cross-cutting context.

        Returns structured subgraph JSON for LLM analysis.
        """
        return await self.execute_with_timeout("""
            MATCH (start)
            WHERE start.name IN $entities
              AND ($channel IS NULL
                   OR start.channel = $channel
                   OR $channel IN start.channels)
            CALL apoc.path.expandConfig(start, {
                minLevel: 1,
                maxLevel: $max_hops,
                uniqueness: 'NODE_GLOBAL',
                limit: $max_nodes
            }) YIELD path
            WITH path, relationships(path) AS rels, nodes(path) AS ns
            WHERE all(r IN rels WHERE
                r.valid_until IS NULL OR r.valid_until > datetime())
            UNWIND rels AS r
            WITH DISTINCT r, startNode(r) AS src, endNode(r) AS tgt,
                 type(r) AS rel_type
            RETURN src.name AS source, src.entity_type AS source_type,
                   tgt.name AS target, tgt.entity_type AS target_type,
                   rel_type, r.context AS context,
                   r.confidence AS confidence,
                   r.evidence AS evidence,
                   r.source_message_id AS source_message_id
            ORDER BY r.confidence DESC
        """, entities=start_entities, channel=channel,
             max_hops=max_hops, max_nodes=max_nodes)

    async def get_episodic_weaviate_ids(self, node_ids: list[int]) -> list[str]:
        """Get Weaviate IDs for enriching graph results with full text."""
        return await self.execute("""
            MATCH (n)-[:MENTIONED_IN]->(e:Event)
            WHERE id(n) IN $ids
            RETURN e.weaviate_id
        """, ids=node_ids)

    async def execute_with_timeout(self, cypher: str, **params) -> list[dict]:
        """Execute with transaction timeout — returns [] on timeout."""
        try:
            async with self.driver.session() as session:
                result = await session.run(cypher, **params,
                                            timeout=self.QUERY_TIMEOUT_MS)
                return await result.data()
        except TransientError:
            logger.warning(f"Graph traversal timed out: {cypher[:80]}...")
            return []  # Retriever falls back to semantic-only
```

---

## Method Reference

| Method | Purpose | Returns on timeout |
|---|---|---|
| `upsert_entity` | Create/update node with scope-aware MERGE | n/a (write) |
| `upsert_relationship` | Create edge with provenance fields | n/a (write) |
| `create_episodic_link` | Bind entity → Event → Weaviate atomic | n/a (write) |
| `traverse` | Bounded N-hop APOC path expansion | not applicable (non-timeout path) |
| `temporal_chain` | SUPERSEDES chain up to 5 hops | `[]` |
| `comprehensive_traverse` | Collect-all subgraph for LLM analysis | `[]` |
| `get_episodic_weaviate_ids` | Fetch Weaviate IDs from Event nodes | n/a (fast lookup) |
| `execute_with_timeout` | Underlying runner with 5s hard limit | `[]` |

All traversal methods return `[]` on `TransientError` (timeout), allowing the query router to fall back to semantic-only results rather than failing the request.
