# Beever Atlas Redesign — End-to-End Test Plan

> **Branch:** `redesign/oss-pipeline-and-wiki`
> **Last updated:** 2026-05-02
> **Author:** test plan covering OSS pipeline redesign (PR-0 → PR-G), production wiring, and the close-the-soak-loop dashboard work.

This test plan walks the redesign end-to-end on a clean install, verifies every shipped feature against its spec contract, and surfaces regressions before staging soak.

Read sections in order. Each phase has a **Goal**, **Pre-conditions**, **Steps**, **Expected**, **Pass/Fail**, and (where useful) **If fail, investigate**. Time estimates assume one operator with no platform pre-connected.

---

## 0. Launch the application

```bash
# Bring up all 7 services (build images from current branch)
docker compose up -d --build

# Tail backend logs in another terminal — leave this running for the entire test
docker compose logs -f beever-atlas

# When done, shut down + wipe data:
docker compose down -v
```

Expect first build to take 3-5 min (Python deps + frontend bundle). Subsequent boots without `--build` are ~30s.

**Open URLs:**
- Web UI — http://localhost:3000
- API — http://localhost:8000/api/health
- Neo4j browser — http://localhost:7474
- Weaviate console — http://localhost:8080

### Frontend bundle gotcha — `--build` is not enough after a frontend change

`docker compose up -d --build` honours BuildKit's layer cache. If a frontend
file changed but `package.json`/lockfile/source paths weren't invalidated in
a way the cache notices, the `web` container can boot with a **stale JS
bundle** that's missing newly-added routes. Symptoms:

- `localhost:3000/admin/wiki-drift` → 404 even though the route is registered
  in `web/src/App.tsx` on the current branch.
- `docker exec beever-atlas-web-1 sh -c 'grep -lc "wiki-drift" /usr/share/nginx/html/assets/*.js'`
  returns 0 hits in any served bundle.

**Fix — force a clean rebuild of just the web service:**

```bash
docker compose build --no-cache web
docker compose up -d web

# Hard-refresh the browser tab (Cmd-Shift-R) to bust the SPA cache.
```

A rebuild also is required when **`web/.env.local`** changes — Vite inlines
env vars (`VITE_*`) at build time, not runtime. So if you set
`VITE_BEEVER_ADMIN_TOKEN` for the first time (Phase 1.5 below), the
`/admin/sources` and `/admin/wiki-drift` pages will keep showing
"Access denied" until you `docker compose build --no-cache web` again.

```bash
# One-shot setup so the admin pages render their real UI:
ADMIN_TOKEN=$(grep "^BEEVER_ADMIN_TOKEN=" .env | cut -d= -f2)
echo "VITE_BEEVER_ADMIN_TOKEN=$ADMIN_TOKEN" > web/.env.local
docker compose build --no-cache web
docker compose up -d web
```

---

## Phase 1 — Boot smoke (10 min)

**Goal:** confirm all 7 services come up healthy on a clean slate; the new admin endpoints exist and return their documented empty-state shapes.

**Pre-conditions:** `.env` populated (`GOOGLE_API_KEY`, `JINA_API_KEY`, `WEAVIATE_API_KEY`, `NEO4J_PASSWORD`, `BRIDGE_API_KEY`, `CREDENTIAL_MASTER_KEY`, `BEEVER_API_KEYS`, `BEEVER_ADMIN_TOKEN`).

### 1.1 All 7 containers healthy

```bash
docker compose ps
```

**Expected:** 7 rows, all `Up X seconds (healthy)`:
- `beever-atlas` (8000)
- `web` (3000)
- `bot` (3001)
- `mongodb` (27017)
- `neo4j` (7474, 7687)
- `weaviate` (8080, 50051)
- `redis` (6380)

**Pass/Fail:** any container in `unhealthy` or restarting → fail. Check `docker compose logs <service>` for the cause.

### 1.2 API health check

```bash
curl -s localhost:8000/api/health | jq
```

**Expected:**
```json
{
  "status": "healthy",
  "components": {
    "weaviate": {"status": "up", ...},
    "neo4j":    {"status": "up", ...},
    "mongodb":  {"status": "up", ...},
    "redis":    {"status": "up", ...}
  }
}
```

**Pass/Fail:** every component must be `"up"`. Latency > 1s on any store is a soft warning.

### 1.3 New admin endpoints (close-the-soak-loop)

```bash
ADMIN_TOKEN=$(grep "^BEEVER_ADMIN_TOKEN=" .env | cut -d= -f2)

# Maintainer metrics — should return zeroed shape (no apply_updates yet)
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  localhost:8000/api/admin/wiki-maintainer/metrics | jq

# Drift summary — empty collection
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  "localhost:8000/api/admin/wiki-drift/summary?days=14" | jq
```

**Expected (1):** all rolling counts `0`, `apply_update_failures: []`, `rewrite_count_by_page_kind` has all 5 buckets at 0, `pending_dirty_pages_per_channel: {}`.

**Expected (2):** `{"channels": [], "pass": false, "data_fresh": false}` with HTTP 200.

**Pass/Fail:** any 500 or different shape → fail.

### 1.4 Web UI loads

Open http://localhost:3000 in browser.

**Expected:** Dashboard renders with empty Channels list, sidebar shows "Connect a platform" CTA.

### 1.5 Admin pages gate on token

Open http://localhost:3000/admin/sources and http://localhost:3000/admin/wiki-drift.

**Expected without admin token:** both pages render "Access denied — set
`VITE_BEEVER_ADMIN_TOKEN` in `web/.env.local`". This is the documented gate
working — admin surfaces are intentionally not viewable by regular users.

**Expected after setting the token (run the one-shot setup from §0):**
- `/admin/sources` → "No sources registered" with a "Register source" CTA
- `/admin/wiki-drift` → "No drift reports in the selected window. Confirm
  `WIKI_DRIFT_AB=true` is set on the staging environment for at least one
  channel."

**Pass/Fail:**
- Without token → "Access denied" UI renders (✓ gate is working)
- With token → empty-state UI renders for both pages (✓ routes registered + admin auth verified end-to-end)
- 404 on `/admin/wiki-drift` → frontend bundle is stale, see §0 "Frontend bundle gotcha"

---

## Phase 2 — First channel + sync (PR-A, PR-B, PR-0) (20 min)

**Goal:** verify the new `channel_messages` collection populates on sync, the cursor advances on fetch success regardless of extraction errors (PR-0 fix), and dual-read fallback works.

### 2.1 Connect a platform (mock adapter for fast E2E)

```bash
# In .env.local for the web app, leave VITE_API_URL=http://localhost:8000
# Use the mock Slack adapter — set in your .env:
echo "SLACK_PLATFORM_MODE=mock" >> .env
docker compose restart beever-atlas
```

Open http://localhost:3000/channels → "Connect Slack" → uses MockAdapter.

**Expected:** `C_MOCK_GENERAL`, `C_MOCK_ENGINEERING`, `C_MOCK_RANDOM` appear as connected channels.

### 2.2 Sync a channel — fast perceived sync

Click `C_MOCK_GENERAL` → "Sync" button. Time how long the sync banner takes to disappear.

**Expected:** ≤ 5 seconds. (Pre-redesign: 30+ seconds because LLM extraction was inline.)

**Pass/Fail:** if sync takes > 30s, decoupling did not land — check `DECOUPLE_EXTRACTION` flag in `.env`.

### 2.3 Verify channel_messages collection populated

```bash
docker exec -it beever-atlas-mongodb-1 mongosh \
  --eval 'db.getSiblingDB("beever_atlas").channel_messages.countDocuments({channel_id: "C_MOCK_GENERAL"})'
```

**Expected:** non-zero count matching the synced messages.

### 2.4 Watch extraction proceed in background

Click "Messages" tab. The new "Enriching" status row should appear at the top showing pending → extracting → done counts.

```bash
curl -s -H "Authorization: Bearer $(grep '^BEEVER_API_KEYS=' .env | cut -d= -f2 | cut -d, -f1)" \
  "localhost:8000/api/channels/C_MOCK_GENERAL/extraction-status" | jq
```

**Expected:** counts shift from `pending: N, done: 0` → `pending: 0, done: N` over ~30-60s. The extraction worker tick interval is 30s.

### 2.5 PR-0 cursor regression check

Stop the stack, simulate a partial extraction failure by injecting a malformed message into `channel_messages`, then re-sync. The cursor should advance regardless. (This is hard to do without dev access — verify via `tests/test_sync_runner.py::test_run_sync_advances_cursor_even_when_batches_fail` instead.)

```bash
docker exec -it beever-atlas-beever-atlas-1 \
  uv run pytest tests/test_sync_runner.py::test_run_sync_advances_cursor_even_when_batches_fail -v
```

**Expected:** test passes.

---

## Phase 3 — ExtractionWorker + CircuitBreaker (PR-B, PR-C) (15 min)

**Goal:** the worker drains the queue with retry + backoff; failed rows surface in the UI; the circuit breaker opens on consecutive 5xx.

### 3.1 Worker metrics endpoint

```bash
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  localhost:8000/api/admin/extraction-worker/metrics | jq
```

**Expected:**
```json
{
  "queue_depth_per_channel": {"C_MOCK_GENERAL": 0},
  "claim_rate_5min": 0.x,
  "claim_rate_15min": 0.x,
  "claim_rate_60min": 0.x,
  "success_rate_5min": 1.0,
  "breaker_state": "closed",
  "recent_failures": []
}
```

**Pass/Fail:** `claim_rate_5min` should be > 0 if a sync ran in the last 5 min.

### 3.2 Failed-batch UI panel

Click the channel's "Settings" tab → "Failed extractions" section.

**Expected:** if no failures, panel says "No failures". If failures, shows `message_id`, `attempt_count`, `last_error`, `next_attempt_at` (relative time).

### 3.3 Auto-retry observable in metrics

Inject a transient failure (e.g. set `GOOGLE_API_KEY=invalid` for 30s, then restore) and watch `recent_failures` populate.

**Expected:** failures land in `recent_failures` list (capped at 10). After the 30s, fresh ticks should drain successfully.

---

## Phase 4 — Wiki generation + WikiMaintainer (PR-E, PR-F, PR-G) (25 min)

**Goal:** per-page `wiki_pages` collection populates after extraction; manual + auto modes both work; lint surfaces tensions; the Maintain Wiki button drains dirty queue.

### 4.1 Wiki pages populate after extraction

After Phase 2's sync completes (extraction status shows all done), open the channel's "Wiki" tab.

**Expected:** ≥ 1 page renders (likely "topic:..." pages from clusters, or "decisions"/"faq"/"action-items" role pages). Each has a title + sections.

### 4.2 Verify wiki_pages collection

```bash
docker exec -it beever-atlas-mongodb-1 mongosh \
  --eval 'db.getSiblingDB("beever_atlas").wiki_pages.find({channel_id: "C_MOCK_GENERAL"}, {page_id: 1, title: 1, version: 1, is_dirty: 1, _id: 0}).pretty()'
```

**Expected:** rows with `version: 1+`, `is_dirty: false`, `title` non-empty.

### 4.3 Maintain Wiki button (manual mode)

In the wiki tab, click "Maintain Wiki" (the toolbar button). It should drain any dirty pages.

**Expected:** if there are dirty pages, they get rewritten. If none, the button shows "Nothing to do".

### 4.4 Lint endpoint surfaces tensions

```bash
curl -s -X POST -H "Authorization: Bearer $USER_TOKEN" \
  localhost:8000/api/channels/C_MOCK_GENERAL/wiki/lint | jq
```

**Expected:** `{findings: [...], runtime_ms: N}`. Findings may be empty for the mock channel.

### 4.5 Per-channel maintenance_mode toggle

Open channel's "Settings" tab → flip `wiki.maintenance_mode` from `manual` → `auto`. Save.

```bash
docker exec -it beever-atlas-mongodb-1 mongosh \
  --eval 'db.getSiblingDB("beever_atlas").channel_policies.findOne({channel_id: "C_MOCK_GENERAL"}, {wiki: 1})'
```

**Expected:** the policy doc has `wiki.maintenance_mode: "auto"` (or null with the global env var taking effect).

### 4.6 Auto mode fires apply_update inline

With `maintenance_mode=auto`, sync the channel again. Watch the wiki pages.

```bash
# Tail extraction events
docker compose logs -f beever-atlas | grep "wiki_maintainer"
```

**Expected:** `wiki_maintainer.on_extraction_done channel=C_MOCK_GENERAL mode=auto affected=N rewritten=N` log lines.

---

## Phase 5 — Push sources (PR-D) (15 min)

**Goal:** register a source, sign a request, ingest events idempotently, rotate the secret.

### 5.1 Register a source via admin UI

http://localhost:3000/admin/sources → "Register source" → `source_id=test-push`, pattern=`*`, description=`Test`.

**Expected:** modal shows the plaintext secret ONCE. Copy it.

### 5.2 Sign + POST events

See `docs/integrations/push-sources.md` for the canonical recipe. Briefly:

```bash
SECRET="<paste from modal>"
BODY='{"events":[{"channel_id":"C_TEST","message_id":"m-001","timestamp":"2026-05-02T10:00:00Z","author":"alice","content":"Hello world"}]}'
TS=$(date +%s)
NONCE=$(openssl rand -hex 16)
SIG=$(echo -n "${TS}.${NONCE}.${BODY}" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -s -X POST localhost:8000/api/sources/test-push/events \
  -H "X-Beever-Timestamp: $TS" \
  -H "X-Beever-Nonce: $NONCE" \
  -H "X-Beever-Signature: $SIG" \
  -H "X-Idempotency-Key: idem-001" \
  -H "Content-Type: application/json" \
  -d "$BODY" | jq
```

**Expected:** `202 Accepted` with `{accepted: 1, deduplicated: 0, ...}`.

### 5.3 Idempotency replay

Re-run the same curl with the same idempotency key.

**Expected:** identical response body, but the event was NOT re-processed. `accepted: 1` is the cached response, not new work.

### 5.4 Rotate secret invalidates old signatures

In the admin UI, click "Rotate" for `test-push`. Get the new secret. Re-run step 5.2 with the OLD secret.

**Expected:** `401 Unauthorized` — old signatures rejected immediately.

---

## Phase 6 — Drift comparator soak (close-the-soak-loop §1, §3) (30 min)

**Goal:** with `WIKI_DRIFT_AB=true`, every successful `apply_update` schedules the comparator, which persists a row to `wiki_drift_reports` AND emits the structured log line. Rate limit prevents bursts.

### 6.1 Enable drift comparator

```bash
# Add to .env:
echo "WIKI_DRIFT_AB=true" >> .env
echo "WIKI_MAINTENANCE_MODE=auto" >> .env
docker compose restart beever-atlas
```

Wait ~10s for the maintainer to re-register on lifespan startup.

### 6.2 Trigger apply_updates by syncing

Sync a channel (mock or real). Watch logs:

```bash
docker compose logs -f beever-atlas | grep -E "wiki_drift_report|wiki_drift_persist|wiki_drift_rate_limited"
```

**Expected:**
- ≥ 1 `event=wiki_drift_report` log line (one per apply_update with new content)
- 0 `wiki_drift_persist_failed` log lines
- After ≥ 2 quick rewrites of the same page within 60s, ≥ 1 `wiki_drift_rate_limited` log line

### 6.3 Verify wiki_drift_reports collection

```bash
docker exec -it beever-atlas-mongodb-1 mongosh \
  --eval 'db.getSiblingDB("beever_atlas").wiki_drift_reports.find({}, {channel_id: 1, page_id: 1, levenshtein_section_p50: 1, ts: 1, _id: 0}).limit(5).pretty()'
```

**Expected:** rows with the documented fields. `ts` is recent.

### 6.4 Verify TTL + indexes

```bash
docker exec -it beever-atlas-mongodb-1 mongosh \
  --eval 'db.getSiblingDB("beever_atlas").wiki_drift_reports.getIndexes()'
```

**Expected:** at least 3 indexes:
- `_id_` (default)
- `wiki_drift_reports_ttl` with `expireAfterSeconds: 2592000`
- `wiki_drift_reports_channel_ts`

### 6.5 Rate-limit semantics

Force two apply_updates on the same page within 60s (e.g. trigger sync, then immediately trigger another with new messages on the same cluster). Confirm only ONE drift report lands.

**Expected:** the second is skipped + logged with `event=wiki_drift_rate_limited since_last_seconds=N`.

### 6.6 Drift dashboard populates

After ≥ 3 reports persist, open http://localhost:3000/admin/wiki-drift.

**Expected:**
- Banner: `PASSING — soak threshold met across N channels` (most likely, since synthetic data is deterministic so drift is low)
- Per-channel rows with `p50_median`, `p95_median`, relative `last_run`, ✓ marker
- Auto-refresh fires every 5 min (verify by leaving the page open and watching `last_run` update)

---

## Phase 7 — Maintainer metrics (close-the-soak-loop §4) (15 min)

**Goal:** `/api/admin/wiki-maintainer/metrics` returns non-zero counters after activity; failure list captures errors; pending-dirty Mongo aggregate works.

### 7.1 Counters increment on apply_update

After Phase 6 (with several apply_updates fired):

```bash
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  localhost:8000/api/admin/wiki-maintainer/metrics | jq
```

**Expected:**
- `apply_update_count_5min: ≥ 1`
- `rewrite_count_by_page_kind`: at least one of `topic`, `entity`, `decisions`, `faq`, `action_items` ≥ 1
- `apply_update_failures: []` (no failures expected on synthetic data)

### 7.2 mark_dirty counter (manual mode)

Switch one channel back to `wiki.maintenance_mode = manual`, sync, then check:

**Expected:** `mark_dirty_count_5min: ≥ 1` (one per page that flipped to dirty).

### 7.3 Failure capture

Inject an LLM failure: temporarily corrupt `GOOGLE_API_KEY`, sync a channel that would trigger apply_update.

**Expected:** `apply_update_failures` gains an entry with `error_class` (e.g. `Exception`, `TimeoutError`), capped at 10.

### 7.4 pending_dirty_pages_per_channel

After 7.2, the same endpoint should show a non-empty `pending_dirty_pages_per_channel`. Click "Maintain Wiki" → re-check.

**Expected:** counts decrement to 0 after maintain drains the queue.

---

## Phase 8 — Touched fact ids restored (close-the-soak-loop §2) (10 min)

**Goal:** consolidation passes a populated `touched_fact_ids` list (not `[]`) to the maintainer, so manual-mode pages get marked dirty.

### 8.1 Verify the bug fix

In manual mode, run consolidation (sync → consolidation auto-fires after sync per `consolidation_strategy=after_every_sync`). Check pages flip to dirty:

```bash
docker exec -it beever-atlas-mongodb-1 mongosh \
  --eval 'db.getSiblingDB("beever_atlas").wiki_pages.countDocuments({channel_id: "C_MOCK_GENERAL", is_dirty: true})'
```

**Expected:** ≥ 1 (the regression `[]` payload would result in 0).

### 8.2 Run the regression test

```bash
docker exec -it beever-atlas-beever-atlas-1 \
  uv run pytest tests/services/test_consolidation_touched_fact_ids.py::test_manual_mode_marks_pages_dirty_via_consolidation_hook -v
```

**Expected:** test passes.

---

## Phase 9 — Multilingual support (15 min)

**Goal:** language detection works on a non-English channel; wiki renders in the configured target language.

### 9.1 Sync a multilingual channel

If you have a Slack/Discord channel with non-English messages, connect it. Otherwise, push synthetic events via Phase 5 with non-English content (e.g. `"こんにちは"` or `"你好"`).

**Expected:** `channel_messages` rows have `source_lang` populated correctly via the BCP-47 detection.

### 9.2 Wiki renders in target language

Open the wiki tab. The pages should render in the language detected (or the `DEFAULT_TARGET_LANGUAGE` env var).

**Expected:** facts are stored in source language; wiki sections rendered in target language.

---

## Phase 10 — MCP server integration (15 min)

**Goal:** MCP tools (including the new `search_memory`, `lint_wiki`, `get_extraction_status`) are reachable; ACL keeps user routes safe.

### 10.1 Enable MCP

```bash
# .env
echo "BEEVER_MCP_ENABLED=true" >> .env
echo "BEEVER_MCP_API_KEYS=mcp-test-key" >> .env
docker compose restart beever-atlas
```

### 10.2 List MCP tools

Use a MCP client (e.g. Claude Desktop, Inspector) to connect to `http://localhost:8000/mcp` with `Authorization: Bearer mcp-test-key`.

**Expected:** 19 tools available (see `tests/integration/test_mcp_e2e_handshake.py::_EXPECTED_TOOL_COUNT`). The new ones to confirm:
- `search_memory`
- `lint_wiki`
- `get_extraction_status`

### 10.3 ACL — MCP token cannot hit user routes

```bash
curl -s -H "Authorization: Bearer mcp-test-key" \
  localhost:8000/api/channels | jq
```

**Expected:** `401` or `403`.

---

## Phase 11 — Recovery + edge cases (20 min)

**Goal:** confirm the system survives the failure modes the redesign was designed to handle.

### 11.1 Stale-extracting sweep

Stop the worker mid-batch (kill the container). Verify rows stuck in `extraction_status="extracting"` get reset to `pending` after the 600s sweep window:

```bash
# Kill backend
docker compose kill beever-atlas

# Wait 11 min (10-min stale window + buffer)
# Restart
docker compose start beever-atlas

# Confirm rows recovered
docker exec -it beever-atlas-mongodb-1 mongosh \
  --eval 'db.getSiblingDB("beever_atlas").channel_messages.countDocuments({extraction_status: "extracting"})'
```

**Expected:** 0 rows stuck in `extracting` after sweep.

### 11.2 Days clamp on drift summary

```bash
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  "localhost:8000/api/admin/wiki-drift/summary?days=10000" | jq
```

**Expected:** still returns valid response (the aggregator was clamped to `days=60` server-side).

### 11.3 Maintainer singleton survives restart

```bash
# Restart backend
docker compose restart beever-atlas

# Check maintainer registered
sleep 10
curl -s -H "X-Admin-Token: $ADMIN_TOKEN" \
  localhost:8000/api/admin/wiki-maintainer/metrics | jq '.apply_update_count_60min'
```

**Expected:** non-error response (i.e. the maintainer singleton re-initializes via lifespan); counters reset to 0 (in-memory state lost, expected).

### 11.4 Drift comparator failure isolation

Temporarily corrupt the regenerate factory path (e.g. delete the `wiki_cache` collection). Trigger `apply_update`.

**Expected:**
- `apply_update` STILL returns successfully (page saved)
- Log line `event=wiki_drift_regenerate_factory_failed` OR `wiki_drift_comparator failed`
- No 500 to the user

### 11.5 Persistence failure does not block log

Temporarily make Mongo unwritable (`mongo:7.0` doesn't easily support read-only mode — alternative: stop Mongo for 5s during an apply_update).

**Expected:** `event=wiki_drift_report` log line still emitted; `wiki_drift_persist_failed` warning surfaces.

---

## Phase 12 — Regression: full test suite (15 min)

**Goal:** the unit + integration suites pass on the current branch.

### 12.1 Backend pytest

```bash
docker exec -it beever-atlas-beever-atlas-1 \
  uv run pytest -q
```

**Expected:** 1900+ pass, ≤ 9 failures (pre-existing flakes in `tests/test_sync_runner.py` and `tests/test_qa_chat_overhaul.py` — confirmed not introduced by this branch).

If flakes drop, no regressions. If new failures appear, investigate before merge.

### 12.2 Frontend vitest

```bash
docker exec -it beever-atlas-web-1 npx vitest run
```

OR locally:
```bash
cd web && npx vitest run
```

**Expected:** 119/119 pass.

### 12.3 Lint + format

```bash
docker exec -it beever-atlas-beever-atlas-1 \
  uv run ruff check src/ tests/

docker exec -it beever-atlas-beever-atlas-1 \
  uv run ruff format --check src/ tests/

cd web && npx tsc --noEmit
```

**Expected:** all clean.

---

## Phase 13 — Frontend journeys (manual, 30 min)

**Goal:** click through the actual user journeys to catch UX regressions the unit tests miss.

### 13.1 First-time user

- [ ] Open http://localhost:3000 with a fresh DB
- [ ] Empty state on Channels page is friendly (not a stack trace)
- [ ] Sidebar "Connect" CTA visible
- [ ] Click through to platform connect flow

### 13.2 Sync flow

- [ ] Trigger sync on a channel
- [ ] Sync banner shows + dismisses cleanly
- [ ] Activity feed shows the sync event
- [ ] Failed-batch panel shows real errors with `last_error` text legible

### 13.3 Wiki flow

- [ ] Wiki tab loads pages without flicker
- [ ] Tensions section renders if any tensions exist
- [ ] Lint button surfaces findings
- [ ] Maintain Wiki button — manual mode drains dirty queue
- [ ] Toggle `maintenance_mode` per-channel via Settings tab
- [ ] Page voice does not visibly drift across multiple maintain runs

### 13.4 Admin flows

- [ ] `/admin/sources` — register, rotate, delete, view replays-24h
- [ ] `/admin/wiki-drift` — banner color matches `pass`, table renders with relative times, auto-refresh works (open + leave for 5+ min)

### 13.5 Ask flow (out-of-scope feature, regression check only)

- [ ] Ask page loads
- [ ] Streaming works (token-by-token visible)
- [ ] Citations resolve
- [ ] Share link generates + opens public-share page

---

## Pass criteria summary

| Phase | Critical | Nice |
|---|---|---|
| 1 Boot smoke | All 7 healthy + endpoints return zeroed shape | — |
| 2 First sync | `channel_messages` populated, ≤ 5s perceived sync | — |
| 3 ExtractionWorker | `claim_rate_5min > 0`, `breaker_state="closed"` | — |
| 4 Wiki | Pages populate, manual + auto both work | — |
| 5 Push sources | Sign + ingest + idempotent replay | Rotation invalidates old sigs |
| 6 Drift soak | Reports persist, rate limit fires, dashboard populates | TTL index verified |
| 7 Maintainer metrics | Counters increment, failures capped at 10 | pending_dirty aggregate works |
| 8 Touched fact ids | Manual mode marks pages dirty (regression fixed) | — |
| 9 Multilingual | Source lang detected, wiki in target lang | — |
| 10 MCP | 19 tools available, ACL holds | — |
| 11 Recovery | Stale-extracting sweep, days clamp, comparator isolation | — |
| 12 Regression suites | Backend pytest, vitest, ruff, tsc all green | — |
| 13 Manual UX | First-time → sync → wiki → admin all click through | — |

**Soft fails (pre-existing branch debt, not blockers):**
- 9 tests in `tests/test_sync_runner.py` + `tests/test_qa_chat_overhaul.py` flake under certain orderings — confirmed not introduced by this branch (verified by stashing close-the-soak-loop changes and reproducing the same set on clean state).

**Hard fails (blockers):**
- Any container `unhealthy` for > 2 min after boot
- Any new test failure in `tests/services/test_wiki_*` or `tests/api/test_admin_wiki_*`
- API `/health` returns any component `down`
- Drift dashboard fails to render banner OR table after ≥ 1 report persists

---

## What's NOT covered by this plan (intentional)

- **Multi-tenancy / ACL / SSO** — out of OSS scope. Defer until customer pull.
- **Production-scale soak (2-week drift comparison across 3 real channels)** — that's the soak runbook (`docs/runbooks/wiki-maintenance-soak.md`), not this plan.
- **Cumulative drift over months** — known gap, tracked as P2 follow-up.
- **Cross-provider LLM failover** — OSS has only Gemini; defer until enterprise tier.
- **Per-channel LLM cost cap** — proposed P0 follow-up; not yet implemented.

---

## Recording your results

Create a copy of this plan as `docs/test-runs/2026-05-02-fresh-test-run.md` and tick off each step. Capture:

- `docker compose ps` output at start
- `/api/health` response
- Both new admin endpoint responses
- Any failures with stderr from `docker compose logs <service>`
- Screenshots of the dashboard at `pass=true` and `pass=false` states

When done, commit the test-run file to the branch.

---

## Related runbooks

- `docs/runbooks/wiki-maintenance-soak.md` — 2-week soak procedure (post-fresh-test, before flipping `auto` default)
- `docs/integrations/openclaw.md` — push integration cookbook
- `docs/integrations/hermes.md` — push integration cookbook
- `docs/integrations/push-sources.md` — vendor-neutral HMAC signing recipe
- `docs/architecture/oss-pipeline.md` — full architecture context
- `HANDOFF.md` — branch state + remaining P1/P2 items
