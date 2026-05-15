# Observability & Operations

---

## Health Endpoints

```python
@app.get("/health")
async def health_check():
    checks = await asyncio.gather(
        check_weaviate(),   # .is_ready()
        check_neo4j(),      # driver.verify_connectivity()
        check_mongodb(),    # ping
        check_gemini(),     # list_models() with 5s timeout
        check_jina(),       # embed test vector with 5s timeout
        check_redis(),      # PING (Chat SDK session state)
    )
    status = "healthy" if all(c.ok for c in checks) else \
             "degraded" if any(c.ok for c in checks if c.critical) else \
             "unhealthy"
    return {"status": status,
            "components": {c.name: c.dict() for c in checks}}
```

Returns `"healthy"` when all components pass, `"degraded"` when at least one critical component is up, and `"unhealthy"` when all critical components are down.

---

## Key Metrics

| Category | Metric | Type | Alert Threshold |
|----------|--------|------|-----------------|
| **Ingestion** | `ingestion.messages.processed` | Counter | Rate drops > 50% |
| | `ingestion.quality_gate.rejected_ratio` | Gauge | > 60% |
| | `ingestion.stage.duration_ms` | Histogram/stage | p95 > 5s |
| | `ingestion.write_intent.pending_count` | Gauge | > 100 |
| | `ingestion.dead_letter.count` | Counter | Any increase |
| **Retrieval** | `retrieval.route.distribution` | Counter | graph > 40% |
| | `retrieval.latency_ms` | Histogram/route | p95 > 3s |
| | `retrieval.empty_results_ratio` | Gauge | > 30% |
| **Stores** | `store.{name}.latency_ms` | Histogram | p95 > 2s |
| | `store.{name}.error_rate` | Gauge | > 1% |
| | `store.neo4j.entity_count` | Gauge | Growth > 1K/day |
| | `store.orphan.count` | Gauge | Any increase |
| **LLM** | `llm.{site}.latency_ms` | Histogram | p95 > 5s |
| | `llm.{site}.error_rate` | Gauge | > 2% |
| | `llm.{site}.token_cost` | Counter | Daily > budget |

Metrics are emitted via OpenTelemetry from `src/beever_atlas/infra/telemetry.py`.

---

## Distributed Tracing

Every ingestion message and query carries a trace ID through all stages and stores:

```python
@tracer.start_as_current_span("ingest_message")
async def process_message(msg: NormalizedMessage):
    span = trace.get_current_span()
    span.set_attribute("message.id", msg.id)
    span.set_attribute("message.channel", msg.channel_id)
    span.set_attribute("message.platform", msg.platform)

    with tracer.start_as_current_span("stage_2_extract"):
        facts = await extract(msg)
    with tracer.start_as_current_span("stage_3_entities"):
        entities = await extract_entities(msg, facts)
    with tracer.start_as_current_span("stage_7_persist"):
        await persist(facts, entities, embeddings)
```

This ensures full end-to-end visibility: a single trace shows the message moving from ingestion through all pipeline stages into both Weaviate and Neo4j.

---

## Backup & Recovery

| Store | Method | Frequency | Retention |
|-------|--------|-----------|-----------|
| Weaviate | `weaviate backup create` → S3 | Daily 3 AM UTC | 30 days |
| Neo4j | `neo4j-admin dump` → S3 | Daily 3 AM UTC | 30 days |
| MongoDB | `mongodump` → S3 | Daily 3 AM UTC | 30 days |

---

## Cross-Store Consistency Checks

A weekly background job validates referential integrity between stores, detecting orphaned references before they affect query results:

```python
class ConsistencyChecker:
    async def check_episodic_links(self):
        """Verify Neo4j Event.weaviate_id → Weaviate object exists."""
        event_ids = await self.neo4j.get_all_weaviate_ids()
        for batch in chunks(event_ids, 100):
            existing = await self.weaviate.batch_exists(batch)
            orphaned = set(batch) - set(existing)
            if orphaned:
                metrics.record("store.orphan.episodic_links", len(orphaned))

    async def check_entity_references(self):
        """Verify Weaviate fact.graph_entity_ids → Neo4j nodes exist."""
        facts = await self.weaviate.get_facts_with_graph_ids()
        for fact in facts:
            for neo4j_id in fact.graph_entity_ids:
                if not await self.neo4j.node_exists(neo4j_id):
                    metrics.record("store.orphan.entity_refs", 1)
```

Orphan counts feed directly into the `store.orphan.count` metric. Any increase triggers an alert. Implemented in `src/beever_atlas/infra/consistency_checker.py`.

---

## ADK Agent Tracing

ADK agents emit OpenTelemetry spans automatically for each agent invocation, tool call, and model request. These integrate with the existing telemetry pipeline — no additional instrumentation is needed for the agent layer.

Each span includes:
- Agent name and type (`LlmAgent`, `SequentialAgent`, `ParallelAgent`, `LoopAgent`)
- Tool invocations with input/output (e.g., `search_weaviate_hybrid`, `traverse_neo4j`)
- Model used (primary or LiteLLM fallback)
- Token counts and latency per model call

This provides full end-to-end visibility from query receipt → agent orchestration → store operations → response generation. See [`13-adk-integration.md`](13-adk-integration.md) for the agent hierarchy.
