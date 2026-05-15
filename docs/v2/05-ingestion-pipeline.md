# Ingestion Pipeline

Messages from any platform enter the pipeline as a `NormalizedMessage` and pass through **6 stages** before being written to both Weaviate and Neo4j. The pipeline is the single write path for all memory — nothing is written to the stores directly.

Target stores: see [`02-semantic-memory.md`](./02-semantic-memory.md) (Weaviate) and [`03-graph-memory.md`](./03-graph-memory.md) (Neo4j).

> **ADK Implementation:** The 6-stage pipeline is orchestrated by the `create_ingestion_pipeline` factory (an ADK `SequentialAgent`), which chains: `PreprocessorAgent` → parallel(`FactExtractorAgent`, `EntityExtractorAgent`) → parallel(`EmbedderAgent`, `CrossBatchValidatorAgent`) → `PersisterAgent`. Store operations are wrapped as ADK `FunctionTool` instances. For large syncs, the Gemini Batch API can be used instead via `BatchPipelineRunner` in `services/batch_pipeline.py`. See [`13-adk-integration.md`](13-adk-integration.md) for the full agent hierarchy.

---

## 5.1 Multi-Platform Adapters

**Chat SDK Evaluation**: The [Vercel Chat SDK](https://chat-sdk.dev/) is TypeScript-only and designed for bot webhooks — it **cannot fetch message history**. We use Python adapters for batch ingestion, with optional Chat SDK for real-time (Phase 2).

```python
@dataclass
class NormalizedMessage:
    """Unified message model across all platforms."""
    content: str
    author: AuthorInfo
    platform: Platform           # slack | teams | discord
    channel_id: str
    channel_name: str
    message_id: str
    timestamp: datetime
    thread_id: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    reactions: list[str] = field(default_factory=list)
    reply_count: int = 0
    raw_metadata: dict = field(default_factory=dict)

class BaseAdapter(ABC):
    @abstractmethod
    async def fetch_history(self, channel_id, since=None, limit=500) -> list[NormalizedMessage]: ...

class SlackAdapter(BaseAdapter):    # slack-sdk (Python)
class TeamsAdapter(BaseAdapter):    # Microsoft Graph API
class DiscordAdapter(BaseAdapter):  # discord.py
```

---

## 5.2 Pipeline: Writes to Both Memory Systems

```
┌─────────────────────────────────────────────────────────────────────┐
│                      INGESTION PIPELINE (6 Stages)                   │
│                                                                      │
│  NormalizedMessage (from any adapter)                                │
│         │                                                            │
│         ▼                                                            │
│  STAGE 1: PREPROCESS                                                 │
│  • Slack mrkdwn → markdown, thread context assembly                  │
│  • Bot/system message filtering                                      │
│  • Media processing: images (Gemini vision), PDFs (pypdf),          │
│    large PDFs chunked into virtual messages                          │
│         │                                                            │
│         ▼  ┌──────────────────────────────────────────────────────┐  │
│  STAGE 2: │  PARALLEL EXTRACTION                                 │  │
│         │ │                                                        │  │
│         │ │  FactExtractorAgent (ADK / Gemini Flash Lite)         │  │
│         │ │  • Extract atomic facts from message + media context  │  │
│         │ │  • Quality gate: score ≥ 0.5, max 2 facts/message    │  │
│         │ │                                                        │  │
│         │ │  EntityExtractorAgent (ADK / Gemini Flash Lite)       │  │
│         │ │  • Extract entities + relationships (guided-flexible)  │  │
│         │ │  • Entity quality gate: confidence ≥ 0.6              │  │
│         │ │  • Filter hypotheticals & sarcasm                     │  │
│         │ └──────────────────────────────────────────────────────┘  │
│         │                                                            │
│         ▼  ┌──────────────────────────────────────────────────────┐  │
│  STAGE 3: │  PARALLEL ENRICHMENT                                 │  │
│         │ │                                                        │  │
│         │ │  EmbedderAgent                                        │  │
│         │ │  • Jina v4 embeddings (2048-dim, named vectors)       │  │
│         │ │  • Multimodal: separate text + image vectors          │  │
│         │ │                                                        │  │
│         │ │  CrossBatchValidatorAgent                             │  │
│         │ │  • Resolve entity aliases across message batches      │  │
│         │ │  • Validate relationship consistency                  │  │
│         │ │  • Merge alias variants discovered across chunks      │  │
│         │ └──────────────────────────────────────────────────────┘  │
│         │                                                            │
│         ▼                                                            │
│  STAGE 4: PERSIST (Outbox Pattern)                                   │
│  │                                                                   │
│  ├──▶ MONGODB: Write intent document (atomic)                        │
│  │    {fact, entities, embeddings, status: {weaviate: pending, ...}} │
│  │                                                                   │
│  ├──▶ WEAVIATE: Upsert atomic fact (idempotent, deterministic UUID)  │
│  │    Mark intent.status.weaviate = "done"                           │
│  │                                                                   │
│  ├──▶ NEO4J: MERGE entities + relationships (idempotent via MERGE)   │
│  │    Mark intent.status.neo4j = "done" (skip if Neo4j unavailable)  │
│  │                                                                   │
│  └──▶ MONGODB: Update sync state, mark intent complete               │
│       Background reconciler retries "pending"/"failed" every 15min   │
└─────────────────────────────────────────────────────────────────────┘
```

> **Batch API mode**: For large initial syncs, the pipeline can run via Gemini Batch API (`use_batch_api=true` on the sync endpoint). `BatchPipelineRunner` submits extraction batches asynchronously, polls for completion, and retries failed batches with smaller sizes. Progress is tracked in MongoDB and visible in the sync status API.

---

## 5.3 Entity Extraction Prompt (Guided-Flexible)

```python
ENTITY_EXTRACTION_PROMPT = """
Extract entities and relationships from this message.

CORE ENTITY TYPES (prefer these when applicable):
- Person: individual (fields: name, role, team)
- Decision: concrete choice (fields: summary, status, rationale, date)
- Project: initiative (fields: name, status, description)
- Technology: tool/framework (fields: name, category)

EXTENSION TYPES (use when content doesn't fit core types):
- Create any type: Team, Meeting, Artifact, Constraint, Deadline, Budget, ...

RELATIONSHIPS:
- Use descriptive verb phrases: DECIDED, WORKS_ON, BLOCKED_BY, OWNS, ...
- NOT limited to a fixed set — use whatever captures the meaning
- Include temporal context when available

EXISTING ENTITIES (reuse names to avoid duplicates):
{existing_entities}

OUTPUT JSON:
{
  "entities": [{"type": "...", "name": "...", "properties": {...},
                "aliases": ["alternative name 1", "@slack_handle", ...]}],
  "relationships": [{"source": "...", "type": "...", "target": "...",
                      "context": "...", "temporal": "current|supersedes:<old>",
                      "evidence": "exact quote or paraphrase from message",
                      "confidence": 0.0-1.0}],
  "confidence": 0.0-1.0
}

ALIAS RULES:
- Map all name variants to a canonical form: "Alice", "@alice", "alice.chen" → "Alice Chen"
- Include Slack handles, nicknames, abbreviated names as aliases
- For projects: "Atlas", "beever-atlas", "the atlas project" → canonical name
"""
```

---

## 5.4 Quality Gate (MemoryQualityGate)

Applied at Stage 2. Rejects low-signal facts before embedding to keep Weaviate clean.

```python
class MemoryQualityGate:
    MIN_LENGTH = 40
    MAX_FACTS_PER_MESSAGE = 2
    MIN_QUALITY_SCORE = 0.5
    VAGUE_PATTERNS = ["the user", "the process", "this was", "it was",
                      "the output", "as mentioned", "was adjusted"]

    def score_fact(self, fact: str) -> float:
        score = 1.0
        if len(fact) < self.MIN_LENGTH: score -= 0.3
        for p in self.VAGUE_PATTERNS:
            if p in fact.lower(): score -= 0.2
        if any(w[0].isupper() for w in fact.split()[1:] if len(w) > 1): score += 0.1
        if fact.startswith(("It ", "This ", "That ")): score -= 0.15
        return max(0.0, min(1.0, score))
```

Facts scoring below `MIN_QUALITY_SCORE` (0.5) are dropped. Each message produces at most 2 facts to prevent over-extraction from verbose messages.

---

## 5.5 Entity Quality Gate (EntityQualityGate)

Applied at Stage 3. Prevents graph pollution from low-confidence or hypothetical entities.

```python
class EntityQualityGate:
    """Quality gate for entity extraction — prevents graph pollution.

    Inspired by Forensic Eyes' per-category confidence thresholds:
    higher bars for high-stakes relationships, lower for casual mentions.
    """
    MIN_ENTITY_CONFIDENCE = 0.6

    # Per-relationship-type confidence thresholds
    # Higher bar for relationships with greater semantic commitment
    RELATIONSHIP_CONFIDENCE = {
        "DECIDED":      0.7,   # Decisions must be clearly stated
        "OWNS":         0.6,   # Ownership/responsibility requires clarity
        "LEADS":        0.6,   # Leadership roles require clarity
        "BLOCKED_BY":   0.6,   # Blockers must be explicit
        "SUPERSEDES":   0.7,   # Temporal evolution must be unambiguous
        "WORKS_ON":     0.4,   # Work associations are common and casual
        "MENTIONS":     0.3,   # Low bar — just needs to be real
        "MEMBER_OF":    0.4,   # Team membership is usually clear
        "USES":         0.4,   # Technology usage is common
        "DEPENDS_ON":   0.5,   # Dependencies should be stated
        "_DEFAULT":     0.5,   # Fallback for LLM-created relationship types
    }

    HYPOTHETICAL_PATTERNS = [
        "maybe", "might", "could", "should we", "what if",
        "let's just", "hypothetically", "joking", "kidding",
    ]

    def filter_entities(self, extraction_result: dict,
                         source_message: str) -> dict:
        """Reject low-confidence entities and hypothetical references."""
        if extraction_result.get("confidence", 0) < self.MIN_ENTITY_CONFIDENCE:
            return {"entities": [], "relationships": []}

        # Raise threshold for hypothetical/sarcastic messages
        msg_lower = source_message.lower()
        threshold = 0.8 if any(p in msg_lower for p in self.HYPOTHETICAL_PATTERNS) \
                       else self.MIN_ENTITY_CONFIDENCE

        valid_entities = [
            e for e in extraction_result.get("entities", [])
            if self._score_entity(e) >= threshold
        ]

        # Only keep relationships where both endpoints survived filtering
        valid_names = {e["name"] for e in valid_entities}
        valid_rels = [
            r for r in extraction_result.get("relationships", [])
            if r["source"] in valid_names and r["target"] in valid_names
               and r.get("confidence", 0.5) >= self.RELATIONSHIP_CONFIDENCE.get(
                   r.get("type", ""), self.RELATIONSHIP_CONFIDENCE["_DEFAULT"])
        ]

        return {"entities": valid_entities, "relationships": valid_rels}

    def _score_entity(self, entity: dict) -> float:
        score = entity.get("confidence", 0.5)
        if entity.get("properties", {}).get("role"): score += 0.1
        if entity.get("properties", {}).get("date"): score += 0.1
        if entity["name"].lower() in ("it", "this", "that", "someone"): score -= 0.5
        return max(0.0, min(1.0, score))
```

---

## 5.6 Contradiction Detection

Contradictory facts are detected and resolved via SUPERSEDES chains. This runs as a **background job every 15 minutes** (not blocking ingestion).

```python
class ContradictionDetector:
    """Detect and resolve contradictory facts via LLM comparison."""

    SIMILARITY_RANGE = (0.70, 0.95)  # Cosine similarity range for candidates
    CONFIDENCE_THRESHOLD = 0.8       # Auto-supersede above this

    async def detect_batch(self):
        """Process recently ingested facts for contradictions."""
        recent = await self.weaviate.get_facts_since(
            minutes_ago=15, has_contradiction_check=False)

        for fact in recent:
            await self._check_contradictions(fact)
            await self.weaviate.mark_contradiction_checked(fact.id)

    async def _check_contradictions(self, new_fact: dict):
        # METHOD 1: Cosine similarity scan (catches rephrased contradictions)
        similar = await self.weaviate.search_similar(
            new_fact["memory"],
            channel_id=new_fact["channel_id"],
            min_similarity=self.SIMILARITY_RANGE[0],
            max_similarity=self.SIMILARITY_RANGE[1],
            exclude_id=new_fact["id"],
            limit=5,
        )

        # METHOD 2: Entity-scoped scan (catches same-topic contradictions
        # regardless of text similarity — e.g., "Alice is auth lead" vs "Bob is auth lead")
        if new_fact.get("graph_entity_ids"):
            entity_related = await self.neo4j.get_facts_for_entities(
                new_fact["graph_entity_ids"],
                exclude_weaviate_id=new_fact["id"])
            similar.extend(entity_related)

        # LLM comparison for each candidate pair
        for candidate in similar:
            result = await self._llm_compare(new_fact, candidate)
            if result["classification"] == "CONTRADICTORY" \
               and result["confidence"] > self.CONFIDENCE_THRESHOLD:
                await self._supersede(older=candidate, newer=new_fact,
                                       reason=result["reason"])

    async def _supersede(self, older, newer, reason):
        # Mark old fact as invalidated in Weaviate
        await self.weaviate.update(older["id"], {
            "invalid_at": datetime.utcnow().isoformat(),
            "superseded_by": newer["id"],
            "supersession_reason": reason,
        })

        # Create SUPERSEDES edge in Neo4j if both have graph entities
        if newer.get("graph_entity_ids") and older.get("graph_entity_ids"):
            await self.neo4j.create_supersedes_edge(
                newer_entity_ids=newer["graph_entity_ids"],
                older_entity_ids=older["graph_entity_ids"],
                reason=reason)
```

**Contradiction comparison prompt:**

```python
CONTRADICTION_PROMPT = """Compare these two facts from the same channel:

EXISTING (created {old_timestamp}):
"{old_memory}"

NEW (created {new_timestamp}):
"{new_memory}"

Classify the relationship:
- CONTRADICTORY: The new fact replaces or invalidates the old fact
- PROGRESSIVE: The new fact builds on or extends the old fact (not a contradiction)
- INDEPENDENT: Different topics, no relationship

Examples:
- "We use JWT with HS256" → "We switched to RS256 for JWT" = CONTRADICTORY
- "We use PostgreSQL for users" → "We use MongoDB for analytics" = INDEPENDENT
- "Alice is exploring Kubernetes" → "Alice deployed to Kubernetes" = PROGRESSIVE
- "Alice is auth lead" → "Bob is the new auth lead" = CONTRADICTORY
- "Sprint deadline is March 15" → "Sprint deadline extended to March 22" = CONTRADICTORY

Respond in JSON: {"classification": "...", "confidence": 0.0-1.0, "reason": "..."}"""
```

**Cost:** ~$0.001 per comparison (Gemini Flash Lite). Typically 0-5 comparisons per new fact. Negligible at scale.

**Retrieval integration:** The `ImprovedSemanticRetriever` filters by `invalid_at IS NULL` — superseded facts are automatically excluded from results without any retrieval code changes.

---

## 5.7 Consolidation Schedule & Triggers

Consolidation builds Tier 0 (channel summaries) and Tier 1 (topic clusters) from Tier 2 (atomic facts). Without consolidation, the wiki has nothing to serve and the "80% free reads" promise doesn't work.

> **ADK Implementation:** Consolidation is orchestrated by the `consolidation_agent` (an ADK `LoopAgent`) containing `cluster_assigner` and `health_checker` sub-agents. See [`13-adk-integration.md`](13-adk-integration.md).

**Three trigger types:**

```python
class ConsolidationService:
    """Manages cluster building, summary updates, and wiki refresh."""

    # TRIGGER 1: After sync (incremental — new facts only)
    async def on_sync_complete(self, channel_id: str):
        """Runs automatically when a channel sync finishes."""
        unclustered = await self.weaviate.get_unclustered_facts(channel_id)
        if not unclustered:
            return

        touched = await self._assign_to_clusters(channel_id, unclustered)
        await self._update_cluster_summaries(channel_id, touched)
        await self._update_channel_summary(channel_id)
        await self.mongo.mark_wiki_dirty(channel_id)

    # TRIGGER 2: Scheduled full rebuild (daily 2 AM UTC)
    @scheduled(cron="0 2 * * *")
    async def daily_full_consolidation(self):
        """Re-evaluates all clusters: coherence, split/merge, summaries."""
        for channel_id in await self.get_active_channels():
            await self._full_reconsolidate(channel_id)
            await self._rebuild_wiki(channel_id)

    # TRIGGER 3: On-demand via API
    async def manual_trigger(self, channel_id: str):
        """Manual refresh for admin use or after bulk operations."""
        await self._full_reconsolidate(channel_id)
        await self._rebuild_wiki(channel_id)

    async def _assign_to_clusters(self, channel_id, new_facts) -> set:
        """Incremental: assign new facts to existing or new clusters."""
        existing = await self.weaviate.get_tier1_clusters(channel_id)
        touched = set()

        for fact in new_facts:
            best_match, best_score = None, 0.0
            for cluster in existing:
                score = await self._topic_similarity(fact, cluster)
                if score > best_score:
                    best_match, best_score = cluster, score

            if best_score > 0.6:
                await self.weaviate.link_fact_to_cluster(fact.id, best_match.id)
                touched.add(best_match.id)
            else:
                # New cluster seed — promoted when 3+ members accumulate
                new_id = await self.weaviate.create_cluster_seed(channel_id, fact)
                touched.add(new_id)

        return touched
```

**Cluster health rules** (applied during daily full reconsolidation):

| Condition | Action |
|-----------|--------|
| Cluster > 100 members | Split via k-means on embeddings into 2-3 sub-clusters |
| Two clusters have summary cosine > 0.85 | Merge into single cluster |
| Cluster coherence score < 0.4 | Re-cluster members from scratch |
| Cluster has 0 members | Delete cluster |

**Wiki dirty flag** — ensures wiki reflects latest changes:

```python
# In wiki_cache.py
async def get_wiki(self, channel_id: str) -> str:
    cached = await self.cache.find_one({"channel_id": channel_id})
    dirty = await self.dirty_flags.find_one({"channel_id": channel_id})

    if cached and (not dirty or not dirty.get("dirty")):
        return cached["content"]  # FREE read — no LLM cost

    # Regenerate: consolidation or entity changes made wiki stale
    wiki = await self.builder.build(channel_id)
    await self.cache.update_one(
        {"channel_id": channel_id},
        {"$set": {"content": wiki, "generated_at": datetime.utcnow()}},
        upsert=True)
    await self.dirty_flags.update_one(
        {"channel_id": channel_id}, {"$set": {"dirty": False}})
    return wiki
```

**What triggers `mark_wiki_dirty`:**
- After sync → consolidation assigns new facts to clusters
- Entity extraction writes new Person/Decision/Technology to Neo4j
- Contradiction detector supersedes a fact
- Manual reconsolidation trigger
