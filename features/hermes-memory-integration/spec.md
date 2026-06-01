# Spec — Atlas as a Hermes Agent memory provider (deep design)

**Supersedes the "gating gap" assumption in `research-brief.md`.** After tracing Atlas internals, the
integration is mostly **assembling existing, production-hardened pieces** plus two small new API
surfaces. This doc is the source of truth for the A0–A4 issues.

## 0. Architecture at a glance

```
Hermes Agent (plugins/memory/atlas/)
  │
  ├─ prefetch(query) ─────────────► POST /api/search            (Bearer)      [EXISTS]
  ├─ atlas_search tool ───────────► POST /api/search            (Bearer)      [EXISTS]
  ├─ sync_turn(user,assistant) ───► POST /api/sources/{id}/events (HMAC)      [EXISTS] async ~30s
  ├─ atlas_remember(verbatim) ────► POST /api/channels/{cid}/memories (Bearer)[NEW A1] sync
  └─ atlas_forget(id) ────────────► DELETE /api/channels/{cid}/memories/{id}  [NEW A1] sync
```

Two write paths by design (different latency/quality tradeoffs):

| Path | Endpoint | Auth | Latency | Extraction | Use |
|------|----------|------|---------|-----------|-----|
| **Conversational** | `POST /api/sources/{source_id}/events` | HMAC `X-Beever-Signature` | 202, ~30s to searchable | Full LLM pipeline (facts+entities+embed+graph) | `sync_turn` background persist |
| **Direct/verbatim** | `POST /api/channels/{cid}/memories` (NEW) | Bearer | sync, immediately searchable | none — embed text, store as-is | explicit `atlas_remember` |

## 1. What already exists (reuse, don't rebuild)

- **Push ingest:** `api/sources.py:122` `post_source_events(source_id, ...) -> PushEventResponse`
  — HMAC-validates, `stores.mongodb.upsert_channel_messages(rows)`, returns `202 {accepted, deduplicated, channel_id, extraction:"queued"}`. Idempotency via `X-Beever-Idempotency-Key`.
- **Extraction pipeline:** `ExtractionWorker.tick()` (`services/extraction_worker.py:210`) polls every `_TICK_SECONDS=30`, runs `BatchProcessor.process_messages()` → `create_ingestion_pipeline()` (`agents/ingestion/pipeline.py`): preprocess → (fact_extractor ∥ entity_extractor) → (embedder ∥ cross_batch_validator) → persister (Weaviate + Neo4j, MongoDB outbox `WriteIntent`).
- **Search/recall:** `api/search.py:50` `POST /api/search` (Bearer, `assert_channel_access`) → `embed_texts([query])` → `stores.weaviate.pseudo_hybrid_search(query_vector, channel_id, limit, threshold)` → `SearchResponse{results:[SearchResultItem], total, query}`.
- **Read:** `GET /api/channels/{cid}/memories` (paginated), `GET .../{memory_id}`.
- **Source registration (admin):** `POST /api/admin/sources` (`require_admin`) creates an `ExternalSource{source_id, secret, allowed_channels_pattern="*"}`; `PATCH .../rotate`, `DELETE .../`.
- **`hermes` is already a known platform** in `PlatformConnection.platform` Literal (`models/platform_connection.py`).
- **Embedding:** `llm/embeddings.py:325` `async embed_texts(texts, *, task=None, settings=None) -> list[list[float]]` (multi-provider via LiteLLM, batched, retry/backoff). Callers send text; Atlas owns embeddings.

## 2. What is genuinely new (issue A1)

### 2a. Per-fact delete  →  `atlas_forget`
- **Store method (new):** `WeaviateStore.delete_fact(fact_id: str) -> bool` — single-object hard delete
  via `collection.data.delete_by_id(uuid=fact_id)`. (Today only `delete_by_channel` exists = whole-channel nuke.)
  - **Decision:** hard delete the Weaviate object. Graph entities are *not* cascade-deleted (shared across facts) — document this. If audit trail is wanted, offer `supersede_fact(old,new)` (already exists, sets `invalid_at`/`superseded_by`) as a soft-forget variant behind a `?soft=true` flag.
- **Endpoint (new):** `DELETE /api/channels/{channel_id}/memories/{memory_id}` → `require_user` +
  `assert_channel_delete_access(principal, channel_id)` (stricter guard already exists,
  `channel_access.py:168`). 404 if `get_fact` returns None; 204 on success.

### 2b. Direct verbatim write  →  immediate `atlas_remember`
- **Endpoint (new):** `POST /api/channels/{channel_id}/memories` → `require_user` + `assert_channel_access`.
  Body `CreateMemoryRequest{memory_text, importance?, topic_tags?, entity_tags?, fact_type?}`.
  Handler: `vec = (await embed_texts([memory_text]))[0]`; build `AtomicFact(id=deterministic, memory_text,
  channel_id, text_vector=vec, tier="atomic", entity_tags=...)`; `await stores.weaviate.upsert_fact(fact)`;
  return `MemoryResponse{id, ...}` 201.
  - Skips LLM extraction (verbatim) and graph entities (no entity extraction) — acceptable for explicit user memories; note as a known limitation (no auto-entity-linking on direct writes).
  - Handle `EmbeddingMigrationInProgress` → 503 (mirror `/api/search`).

## 3. Onboarding & credentials (issue A0 — NEW, was missing)

A Hermes user needs **two** secrets, because ingest and query use different auth:
1. **HMAC source secret** — for `POST /api/sources/{id}/events`. Provisioned via `POST /api/admin/sources`
   (admin) → `ExternalSource{source_id:"hermes-<id>", secret, allowed_channels_pattern}`.
2. **Bearer API key** — for search/read/write/delete. An entry in `BEEVER_API_KEYS`.

Tasks:
- A small **onboarding helper** (Atlas side): either a doc'd admin runbook, or a convenience endpoint
  `POST /api/admin/sources` already covers (1); document (2) is operator-managed env.
- **`hermes memory setup` flow** (Hermes side): collect `base_url`, `source_id`, `hmac_secret` (secret),
  `api_key` (secret), `channel_id`. Persist non-secrets to `Path(hermes_home)/atlas.json`; secrets → `.env`
  (`ATLAS_HMAC_SECRET`, `ATLAS_API_KEY`).
- **Channel scoping:** `channel_id` is free-form for ingest (gated by source's `allowed_channels_pattern`).
  For **search/delete**, `assert_channel_access` checks `PlatformConnection` ownership:
  - Single-tenant (default, `BEEVER_SINGLE_TENANT=true`): a `user`/`mcp` principal CAN access a channel
    with no owning connection (browsing path) → works out of the box.
  - Multi-tenant: must create a `PlatformConnection(platform="hermes", owner_principal_id=<user:hash>,
    selected_channels=[channel_id])`. Add this to onboarding for multi-tenant deployments.

## 4. The async-lag design tension (drives A2 + A4)

`sync_turn` → push events → extraction runs on a 30s tick → fact becomes searchable. So:
- **Cross-session / long-term memory:** push path is perfect (high-quality extracted facts).
- **Within-session immediate recall:** push path is too slow. The provider should ALSO write important
  user statements via the **direct verbatim path** so `prefetch` on the very next turn can recall them.
- **Provider policy (A2):** `sync_turn` → push (background, daemon thread, non-blocking per dev-guide
  threading contract); `atlas_remember` tool + a heuristic "the user asserted a durable fact" → direct write.
- **A4 E2E must account for the lag:** test the direct path for immediate recall; for the push path, drive
  the worker tick explicitly (call `ExtractionWorker.tick(channel_id)`) rather than sleeping 30s.

## 5. Threshold & search semantics (get this right in the client/contract)

`pseudo_hybrid_search` uses cosine distance; `similarity = 1.0 - distance`; results below `threshold`
filtered, superseded facts (`invalid_at` set) excluded. `SearchRequest.threshold` default 0.7,
range [0,1]. `SearchResultItem`: `id, memory_text, quality_score, topic_tags, entity_tags, importance,
author_name, message_ts, channel_id, similarity_score`. Default `limit` 20 (≤100).

## 6. Provider plugin shape (A2) — confirmed against the ABC

`plugins/memory/atlas/`: `__init__.py` (`AtlasMemoryProvider` + `register(ctx)`), `plugin.yaml`
(`hooks: [prefetch, sync_turn, on_memory_write, on_session_end]`), `cli.py` (`hermes atlas status|config`),
`README.md`.

- `is_available()` → all of base_url/api_key/(source_id+hmac_secret)/channel_id present; no network.
- `initialize(session_id, **kwargs)` → build `AtlasClient` (A3); map `session_id`→`channel_id`
  (persist in `atlas.json`).
- `prefetch(query, *, session_id)` → `client.search(query, channel_id, limit, threshold)`; format top-N
  `memory_text` (with `similarity_score`) into a recall block string.
- `sync_turn(user, assistant, *, session_id, messages)` → **daemon thread**, join prior with timeout;
  `client.ingest_events(channel_id, [user, assistant])` (HMAC push).
- `on_memory_write(action, target, content)` → mirror to direct write.
- tools: `atlas_search(query, limit?)`, `atlas_remember(content, mode=raw|extract)`,
  `atlas_forget(memory_id)`, `atlas_profile()`.
- **OPEN:** exact `get_tool_schemas()` format (OpenAI-style fn defs?) — confirm against Hermes source
  before A2 build; the dev guide doesn't pin the shape.

## 7. Open decisions for the human gate

1. `atlas_remember` default mode: **raw/verbatim (immediate)** vs **extract (queued, richer)**. Rec: `raw` default, `mode=extract` opt-in.
2. Delete default: **hard** vs **soft/supersede**. Rec: hard, with `?soft=true`.
3. Multi-tenant onboarding: auto-create `PlatformConnection(platform="hermes")` on first write, or require operator setup? Rec: helper script, not auto.
4. Should `sync_turn` push every turn, or batch/threshold by importance to control Atlas extraction cost? Rec: every turn (idempotency dedups), revisit if cost is high.
5. One Atlas `channel_id` per Hermes session vs per Hermes "profile"/directory? Rec: per Hermes profile (stable long-term memory), configurable.
