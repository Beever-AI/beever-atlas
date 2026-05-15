# OSS Pipeline + LLM Wiki — Architecture

The Beever Atlas OSS pipeline turns chat conversations into a Karpathy-style
LLM Wiki: a maintained personal-intelligence document served back to AI
agents (OpenClaw, Hermes Agent) as durable memory. This page summarizes the
9-PR redesign (PR-0 through PR-G) for future contributors.

> Spec: `openspec/changes/oss-pipeline-and-wiki-redesign/`

## Outcomes the redesign delivers

1. **Faster + more stable pipeline.** Sync (fetch + persist) finishes in
   seconds; LLM extraction proceeds in the background. A Gemini 503 storm
   no longer kills a sync, never blocks the cursor, never produces a wall
   of identical errors in the UI.

2. **LLM Wiki as a bookkeeper.** Pages compound instead of regenerating
   from scratch. New facts route deterministically to the affected pages,
   rewriting only the changed sections — title, slug, and page voice are
   preserved across edits. Tensions (contradictions) surface inline.

3. **Errors don't kill the pipeline.** Cursor advances independent of
   extraction outcome. Failed batches are recorded as diagnostics, not
   lost. The CircuitBreaker is centralized + injectable. Failed rows
   auto-retry with exponential backoff. Idempotent re-extraction via
   content-hash fact ID.

4. **Push-ready for OpenClaw / Hermes.** A signed
   `POST /api/sources/{source_id}/events` endpoint with HMAC-SHA256 +
   idempotency-key + 5-min skew window. External agent runtimes push
   messages directly into the same `channel_messages` collection that
   pull adapters use.

## Out of scope (deferred to enterprise tier)

- Multi-tenancy, ACL, SSO, audit log, GDPR cascade.
- Non-chat extractors (Doc / Ticket / Row / BigQuery).
- Cross-provider failover wiring (Claude, OpenAI) — only the abstraction
  is built.
- Real durable queue (Redis / BullMQ / SQS) — MongoDB is sufficient at
  OSS scale.
- Obsidian / markdown export — the wiki UI lives in Beever Atlas's web app.
- `KnowledgeAtom` rename of `channel_messages` — explicitly rejected by
  Opus consensus.

## The 9 PRs

| PR | Capability | Flag (default) |
|---|---|---|
| **PR-0** | Cursor advances on fetch success regardless of extraction errors | (no flag) |
| **PR-A** | Durable Message Store + Source protocol seam | `READ_FROM_MESSAGE_STORE` (OFF) |
| **PR-B** | Background `ExtractionWorker` + content-hash fact ID + frontend dedupe | `DECOUPLE_EXTRACTION` (OFF) |
| **PR-C** | Injectable `CircuitBreaker` + provider failover seam + auto-retry | (no env flag — failover is out of OSS scope; enterprise tier flips `llm/provider.py:_FAILOVER_ENABLED` in code) |
| **PR-D** | Push-source HMAC ingest endpoint | (no flag — gated by per-source registration) |
| **PR-E** | Per-page wiki page-store split | `PER_PAGE_WIKI` (OFF) |
| **PR-F** | `WikiMaintainer` service (incremental maintainer) | `WIKI_MAINTENANCE_MODE` (`manual`) |
| **PR-G** | Wiki lint endpoint + tensions surfacing | (no flag) |

All flags default OFF so the branch is safe to merge to `main` without
behavior change. Production rollout flips the flags in the order above
per the runbook in `tasks.md` section 2g.1.

### Operator-flippable env vars (6)

The redesign exposes exactly **six** env vars to operators — every other
tuning value lives as a module constant in code. The principle: an env
var exists iff a non-developer would change it without recompiling
during incident response or a planned rollout.

| Env var | Purpose | Owner module |
|---|---|---|
| `READ_FROM_MESSAGE_STORE` | Chat-platform read-path kill switch | `api/channels.py` |
| `READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES` | File-import read-path kill switch (kept separate for granular rollback) | `api/channels.py` |
| `WRITE_DUAL_FILE_IMPORTS` | Temporary dual-write safety net (deleted after rollout) | `api/imports.py` |
| `DECOUPLE_EXTRACTION` | Sync→Worker primary lever | `services/sync_runner.py` |
| `PER_PAGE_WIKI` | Wiki schema migration kill switch | `wiki/cache.py` |
| `WIKI_MAINTENANCE_MODE` | Manual vs auto wiki maintenance (default `"manual"`) | `services/wiki_maintainer.py` |

Tuning constants that are NOT env vars (and where to find them):
- Worker tick / stale / max-retries: `services/extraction_worker.py:_TICK_SECONDS`, `_STALE_SECONDS`, `_MAX_RETRIES`
- Circuit breaker cooldown: `services/circuit_breaker.py:_DEFAULT_COOLDOWN_SECONDS`
- LLM failover enablement + map: `llm/provider.py:_FAILOVER_ENABLED`, `_FALLBACK_MAP` (out of OSS scope; enterprise tier flips in code)

## Data flow (with all flags ON)

```
                   pull adapters             POST /api/sources/{id}/events
                   (Slack, Discord, ...)     (OpenClaw, Hermes)
                          │                         │
                          ▼                         ▼  HMAC-verified
              ┌─────────────────────────────────────────────┐
              │   channel_messages   collection (Mongo)     │
              │   key: (source_id, channel_id, message_id)  │
              │   extraction_status: pending → extracting   │
              │                                  → done|failed
              └─────────────────────────────────────────────┘
                          │
                          │  ExtractionWorker.tick() — APScheduler 30s
                          │  find_one_and_update atomic claim
                          ▼
              ┌─────────────────────────────────────────────┐
              │  6-stage ADK pipeline (preserved unchanged) │
              │  preprocessor → fact_extractor → entity_*   │
              │  → embedder → cross_batch_validator         │
              │  → persister                                │
              └─────────────────────────────────────────────┘
                          │              │
                          │              └─→ Weaviate / Neo4j / Nebula
                          ▼
              ┌─────────────────────────────────────────────┐
              │  on_extraction_done(channel_id, fact_ids)   │
              └─────────────────────────────────────────────┘
                          │
                          ▼
              ┌─────────────────────────────────────────────┐
              │  WikiMaintainer.plan_updates(facts)         │
              │   • cluster_id  → topic:<slug>              │
              │   • entity_tags → entity:<slug>             │
              │   • fact_type   → decisions / faq / ai      │
              │  (deterministic — NO LLM call)              │
              └─────────────────────────────────────────────┘
                          │
            mode=manual:  │  mode=auto:
            mark dirty    │  apply_update per page (1 LLM call each)
                          │
                          ▼
              ┌─────────────────────────────────────────────┐
              │  wiki_pages collection (Mongo)              │
              │  one doc per (channel_id, target_lang,      │
              │   page_id) with sections, version, tensions │
              └─────────────────────────────────────────────┘
                          │
                          ▼  POST /api/channels/{id}/wiki/lint
              ┌─────────────────────────────────────────────┐
              │  Lint findings — orphan / stale / dup /     │
              │   coherence (1 bounded LLM call per page)   │
              └─────────────────────────────────────────────┘
```

## Production rollout sequence (10 steps)

1. **PR-0 cursor hotfix** — soak 24h on `main`.
2. **PR-A migration** — `python -m beever_atlas.scripts.migrate_imported_messages_to_channel_messages --dry-run`, then real run.
3. **`READ_FROM_MESSAGE_STORE=true`** in staging → soak 48h → production.
4. **`READ_FILE_IMPORTS_FROM_CHANNEL_MESSAGES=true`** in staging → soak 48h → production.
5. **`WRITE_DUAL_FILE_IMPORTS=false`** in staging → soak 1 week → production.
6. **`DECOUPLE_EXTRACTION=true`** in staging → soak 48h → production.
7. **Skip — failover is out of OSS scope.** Enterprise tier flips `llm/provider.py:_FAILOVER_ENABLED = True` and populates `_FALLBACK_MAP` with their multi-provider routing in code, then redeploys.
8. **`PER_PAGE_WIKI=true`** in staging → soak 48h → production.
9. **`WIKI_MAINTENANCE_MODE=auto`** only after 2-week A/B comparison on three real channels.
10. **Drop `imported_messages` collection** — irreversible; final 1-week soak window.

Steps 1–8 are reversible via flag flip; steps 9–10 are not.

## Strategic constraints (DO NOT re-derive)

- OSS is **personal intelligence + Karpathy-style LLM Wiki**. The wiki UI
  lives in Beever Atlas's web app — there is no Obsidian export.
- The 6-stage ADK extraction pipeline at `agents/ingestion/` is preserved
  unchanged. This redesign is re-plumbing, not a rewrite.
- DO NOT rename `channel_messages` to `knowledge_atoms` (Opus consensus
  rejected as cosmetic churn).
- All behavioral changes ship behind feature flags, default OFF, with
  dual-read fallback for migration safety.
- Activity log `event_type` stays binary (`sync_completed` /
  `sync_failed`) — the third state is encoded in `failed_batches`.
- Platform connectors are commodity. The IP is the agent memory layer +
  LLM Wiki. OpenClaw and Hermes Agent own platform reach long-term.

## Where to look

- **OpenSpec change directory:** `openspec/changes/oss-pipeline-and-wiki-redesign/`
  - `proposal.md` — what changed and why
  - `design.md` — design decisions D1–D12
  - `tasks.md` — sub-task progress dashboard
  - `specs/<capability>/spec.md` — per-capability scenarios
- **Code:**
  - Sync: `services/sync_runner.py`, `services/scheduler.py`
  - Worker: `services/extraction_worker.py`
  - Breaker: `services/circuit_breaker.py`, `llm/provider.py`
  - Push: `api/sources.py`, `services/push_hmac.py`
  - Wiki: `wiki/page_store.py`, `services/wiki_maintainer.py`,
    `services/wiki_lint.py`, `api/wiki.py`
- **Tests:**
  - Per-capability under `tests/services/`, `tests/api/`, `tests/wiki/`,
    `tests/stores/`, `tests/llm/`.
  - Frontend: `web/src/lib/__tests__/dedupeErrors.test.ts` and the
    `useExtractionStatus` hook + `useSync` integration in
    `web/src/hooks/`.
