# Hermes Agent ↔ Beever Atlas — Memory Integration Research Brief

**Date:** 2026-06-01
**Branch:** `feat/hermes-memory-integration` (off `origin/main` @ 5af9651)
**Status:** Research + framing only (no build)

## 1. The opportunity

Nous Research's **Hermes Agent** supports **pluggable external memory providers** (Honcho, Mem0,
Supermemory, OpenViking, Hindsight, Holographic, RetainDB, ByteRover, Memori). Exactly **one**
external provider can be active at a time, layered on top of Hermes' built-in `MEMORY.md` / `USER.md`.

Per Atlas product strategy, **Atlas Core is memory infrastructure**. So the strategic fit is direct:
**ship a Hermes memory-provider plugin backed by Beever Atlas**, making Atlas a drop-in long-term
memory backend for any Hermes Agent user.

- Hermes provider docs: https://hermes-agent.nousresearch.com/docs/user-guide/features/memory-providers
- Hermes plugin dev guide: https://hermes-agent.nousresearch.com/docs/developer-guide/memory-provider-plugin

## 2. How Hermes expects a provider to behave

A provider subclasses `agent.memory_provider.MemoryProvider` and lives at
`plugins/memory/<name>/` (`__init__.py` with `register(ctx)`, `plugin.yaml`, optional `cli.py`, `README.md`).

**Required:** `name` (property), `is_available()`, `initialize(session_id, **kwargs)` (kwargs include
`hermes_home`), `get_tool_schemas()`, `handle_tool_call(tool_name, args, **kwargs)`,
`get_config_schema()`, `save_config()`.

**Optional lifecycle hooks** (this is where the value is):

| Hook | When | Maps to Atlas |
|------|------|---------------|
| `system_prompt_block()` | startup | static provider blurb |
| `prefetch(query, *, session_id)` | before each turn | `POST /api/search` → inject recalled facts |
| `queue_prefetch(query)` | after turn | pre-warm search cache |
| `sync_turn(user, assistant, *, session_id, messages)` | after each response, **non-blocking** | **ingest turn → Atlas (gap, see §4)** |
| `on_session_end(messages)` | session end | final fact extraction/flush |
| `on_pre_compress(messages)` | before context discard | save insights |
| `on_memory_write(action, target, content)` | built-in memory write | mirror into Atlas |
| `shutdown()` | process exit | close client |

`sync_turn` **must be non-blocking** (daemon thread; the dev guide is explicit).

## 3. What Atlas exposes today

FastAPI service (`src/beever_atlas/server/app.py:app`), Python 3.12 + `uv`, run via `make dev` /
`docker compose up`. Auth = `Authorization: Bearer <key>` against `BEEVER_API_KEYS`
(`src/beever_atlas/infra/auth.py`). Memories are scoped by **`channel_id`** (the natural
namespace/workspace for a Hermes session).

**Public memory API (read side — exists):**

| Op | Method | Route |
|----|--------|-------|
| List memories | GET | `/api/channels/{channel_id}/memories` (page, limit, topic, entity, importance, since, until → `PaginatedFacts`) |
| Get one | GET | `/api/channels/{channel_id}/memories/{memory_id}` |
| Semantic search | POST | `/api/search` (`SearchRequest{query, channel_id?, limit, threshold}` → `SearchResponse`) |

Data model = `AtomicFact` (`src/beever_atlas/models/domain.py`): `memory_text`, `quality_score`,
`tier`, `topic_tags`, `entity_tags`, `importance`, `text_vector`, knowledge-graph entity ids, etc.
Storage: **Weaviate** (vectors) + **Neo4j/Nebula** (graph) + **MongoDB** (metadata). Embeddings via
LiteLLM, multi-provider (`EMBEDDING_PROVIDER`/`EMBEDDING_MODEL`, default Jina v4 @ 2048-dim).

## 4. The gap that gates everything

**Atlas has no public HTTP write/ingest or delete endpoint for memories.** All ingestion happens
*inside* the Atlas process via ADK extraction pipelines. So Hermes' `sync_turn()` and
`on_memory_write()` — the "persist this conversation" hooks — currently have **nothing to call**.

This makes the Atlas-side ingest API the **first, gating** piece of work.

## 5. Proposed decomposition (→ Linear: parent + 4 sub-issues)

- **A1 — Atlas: public memory write + delete API (GATING).** Add `POST /api/channels/{channel_id}/memories`
  (ingest a turn or a raw fact; run it through extraction/embedding) and
  `DELETE /api/channels/{channel_id}/memories/{memory_id}`. Auth-scoped, channel-scoped, TDD.
- **A2 — Hermes: `atlas` memory-provider plugin.** Implement `MemoryProvider`: `prefetch`→search,
  `sync_turn`→A1 ingest (daemon thread), tools `atlas_remember` / `atlas_search` / `atlas_forget` /
  `atlas_profile`, `get_config_schema()` for `base_url` / `api_key` (secret) / `channel_id`.
- **A3 — Shared Python Atlas client + integration contract.** Thin `httpx` client (search/ingest/get/delete)
  reused by the plugin; document the embedding-consistency contract (Hermes need not embed — Atlas
  owns embeddings server-side) and channel→session scoping.
- **A4 — E2E round-trip + docs.** Hermes turn → A1 ingest → recall via `prefetch`; provider README +
  Atlas API docs; `hermes memory setup` selectable.

## 6. Open questions for spec stage

- Should ingest accept **raw turns** (Atlas extracts facts) or **pre-formed facts**, or both? (Recommend both: `mode=extract|raw`.)
- Per-session `channel_id` provisioning: auto-create channel on first write, or require pre-provisioned channel?
- Delete semantics: hard delete vs. tombstone (graph entities may be shared across facts).
- Rate limits / quota for the public write path (Atlas Core has a credits model).
