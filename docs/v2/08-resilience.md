# Resilience & Degradation Design

The v2 architecture depends on 6 external services: Weaviate, Neo4j, MongoDB, Gemini, Jina, and Tavily. Any component failure must degrade gracefully — not cause total system failure.

---

## 12.1 Dependency Health Registry

Each external dependency gets a circuit breaker with three states: `CLOSED` (healthy), `OPEN` (failing, requests blocked), and `HALF_OPEN` (probing for recovery).

```python
class DependencyHealth:
    """Circuit breaker per external dependency (CLOSED → OPEN → HALF_OPEN)."""

    DEPENDENCIES = {
        "weaviate":  {"critical": True,  "timeout_s": 5},
        "neo4j":     {"critical": False, "timeout_s": 5},
        "mongodb":   {"critical": True,  "timeout_s": 5},
        "gemini":    {"critical": True,  "timeout_s": 10},
        "jina":      {"critical": False, "timeout_s": 10},
        "tavily":    {"critical": False, "timeout_s": 5},
        "redis":     {"critical": False, "timeout_s": 2},   # Chat SDK state
    }

    async def check(self, name: str) -> bool:
        """Returns True if dependency is available."""
        if self.states[name] == CircuitState.OPEN:
            if time_since_open > RECOVERY_WINDOW:  # e.g., 30s
                self.states[name] = CircuitState.HALF_OPEN
                return True  # Probe with one request
            return False
        return True

    def record_failure(self, name: str):
        """After 3 consecutive failures, open the circuit."""
        self.failure_counts[name] += 1
        if self.failure_counts[name] >= 3:
            self.states[name] = CircuitState.OPEN
            logger.error(f"Circuit OPEN for {name}")

    def record_success(self, name: str):
        """Reset failure count, close circuit if half-open."""
        self.failure_counts[name] = 0
        if self.states[name] == CircuitState.HALF_OPEN:
            self.states[name] = CircuitState.CLOSED
```

Implemented in `src/beever_atlas/infra/health_registry.py`.

---

## 12.2 Degradation Matrix

| Component Down | Ingestion Impact | Retrieval Impact | Behavior |
|----------------|-----------------|------------------|----------|
| **Neo4j** | Stage 3 skipped; facts stored in Weaviate only; entities queued for backfill | `route=graph` → reclassify as `route=semantic` | Wiki People/Decisions show "temporarily unavailable" |
| **Gemini** | Messages queued in dead letter queue | ADK agents fall back to Claude models via LiteLLM; if all LLMs fail, return cached wiki only | Alert fired; retry on recovery |
| **Redis** | No impact (batch ingestion unaffected) | No impact (MCP queries unaffected) | Chat SDK bot offline; users see "bot unavailable" in Slack/Teams/Discord |
| **Jina** | Embeddings queued; facts stored text-only in Weaviate | Existing embeddings work; new facts use BM25-only | Backfill embeddings when Jina recovers |
| **Tavily** | No impact | Silently drop external sub-queries; return internal-only results | User sees "external search unavailable" note |
| **Weaviate** | Full ingestion paused (queue in MongoDB) | Return cached wiki; graph-only for relational queries | Critical alert — system severely degraded |
| **MongoDB** | Full system paused | Read-only from Weaviate/Neo4j if cached connections survive | Critical alert — system offline |

---

## 12.3 LLM Fallback via ADK + LiteLLM

All LLM calls are handled by [Google ADK](https://google.github.io/adk-docs/) agents. Model fallback is configured via ADK's native [LiteLLM](https://docs.litellm.ai/) integration rather than a custom provider class. Circuit breakers (Section 12.1) still apply at the dependency health level.

Each ADK agent is configured with a primary model. When the primary is unavailable (timeout, rate limit, circuit open), LiteLLM transparently routes to the fallback model:

| Agent Tier | Primary | Fallback (via LiteLLM) |
|-----------|---------|------------------------|
| Fast (routing, extraction, classification) | `gemini-2.0-flash-lite` | `anthropic/claude-haiku-4-5` |
| Quality (response generation, wiki synthesis) | `gemini-2.0-flash` | `anthropic/claude-sonnet-4-6` |

### Fallback Chain Per ADK Agent

Each call site below corresponds to a specific ADK agent. The primary/fallback models are configured on the agent definition, with LiteLLM handling the failover transparently.

| ADK Agent | Primary | Fallback | Last Resort |
|-----------|---------|----------|-------------|
| `query_router_agent` | Gemini Flash Lite | Claude Haiku | Regex fast-path classifier |
| `fact_extractor_agent` (Stage 2) | Gemini Flash Lite | Claude Haiku | Dead letter queue |
| `entity_extractor_agent` (Stage 3) | Gemini Flash Lite | Claude Haiku | Skip (Weaviate-only) |
| Classification (Stage 4) | Gemini Flash Lite | Rule-based tagger | Skip (no tags) |
| `response_agent` | Gemini Flash | Claude Sonnet | Return raw results |
| `consolidation_agent` (Wiki) | Gemini Flash Lite | Claude Haiku | Serve stale cache |

Model configuration is defined in the ADK agent declarations. See `src/beever_atlas/agents/` and [`13-adk-integration.md`](13-adk-integration.md).

---

## 12.4 Ingestion Pipeline Resilience

Each pipeline stage is independently skippable. If a non-critical stage fails, the pipeline continues:

```python
async def ingest_message(self, msg: NormalizedMessage):
    # Stage 1: Preprocess (required)
    preprocessed = await self.preprocessor.process(msg)

    # Stage 2a: Extract facts (required — queue to DLQ on failure)
    try:
        facts = await self.extractor.extract(preprocessed)
    except LLMUnavailableError:
        await self.dead_letter_queue.enqueue(msg)
        return

    # Stage 2b: Entity extraction (optional — skip if Neo4j/LLM down)
    entities = []
    if await self.health.check("neo4j") and await self.health.check("gemini"):
        try:
            entities = await self.entity_extractor.extract(preprocessed, facts)
        except Exception as e:
            logger.warning(f"Entity extraction failed, continuing: {e}")
            await self.backfill_queue.enqueue("entities", msg.id, preprocessed)

    # Stage 3: Embed (optional — queue if Jina down)
    embeddings = None
    if await self.health.check("jina"):
        embeddings = await self.embedder.embed(facts)
    else:
        await self.backfill_queue.enqueue("embeddings", msg.id, facts)

    # Stage 4: Persist via outbox pattern
    await self.persister.persist(facts, entities, embeddings)
```

---

## 12.5 Write Safety — Outbox Pattern

Stage 7 uses a MongoDB outbox pattern for cross-store write safety. Writes are committed as a single intent document first, then fanned out to each store independently and idempotently.

```python
class OutboxPersister:
    """Two-phase persist: commit intent to MongoDB first, then fan out."""

    async def persist(self, facts, entities, embeddings, tags) -> str:
        # PHASE 1: Write intent (single MongoDB transaction)
        intent = WriteIntent(
            id=deterministic_uuid(facts),
            facts=facts, entities=entities,
            embeddings=embeddings, tags=tags,
            status={"weaviate": "pending",
                    "neo4j": "pending" if entities else "skipped",
                    "state": "pending"},
            retry_count=0,
        )
        await self.mongo.write_intents.insert_one(intent.dict())

        # PHASE 2: Fan out (idempotent, independently retryable)
        await self._fan_out(intent)
        return intent.id

    async def _fan_out(self, intent: WriteIntent):
        # Weaviate — idempotent via deterministic UUID
        if intent.status["weaviate"] == "pending":
            try:
                await self.weaviate.upsert(intent.facts, intent.embeddings)
                await self._mark(intent.id, "weaviate", "done")
            except Exception:
                await self._mark(intent.id, "weaviate", "failed")

        # Neo4j — idempotent via MERGE semantics
        if intent.status["neo4j"] == "pending":
            try:
                for entity in intent.entities:
                    await self.neo4j.upsert_entity(entity)
                await self._mark(intent.id, "neo4j", "done")
            except Exception:
                await self._mark(intent.id, "neo4j", "failed")

        # MongoDB sync state — final step
        await self._update_sync_state(intent)
        await self._mark(intent.id, "state", "done")
```

### Background Write Reconciler

Runs every 15 minutes to retry any incomplete cross-store writes:

```python
class WriteReconciler:
    """Retry incomplete cross-store writes."""

    async def reconcile(self):
        stale = await self.mongo.write_intents.find({
            "$or": [
                {"status.weaviate": {"$in": ["pending", "failed"]}},
                {"status.neo4j": {"$in": ["pending", "failed"]}},
            ],
            "created_at": {"$lt": now() - timedelta(minutes=5)},
            "retry_count": {"$lt": 5},
        }).to_list()

        for intent in stale:
            await self.persister._fan_out(WriteIntent(**intent))
            await self.mongo.write_intents.update_one(
                {"id": intent["id"]}, {"$inc": {"retry_count": 1}})
```

Implemented in `src/beever_atlas/services/reconciler.py`. Outbox intent documents are persisted via `services/batch_processor.py` and `agents/ingestion/persister.py`.
