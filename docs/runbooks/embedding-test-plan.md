# Embedding-provider feature — manual test plan

**Branch**: `feat/litellm-embedding-provider` (PR #154)
**Audience**: Operator validating the feature on a live install before merge.
**Scope**: Both the no-change path AND the marquee Jina → OpenAI/Gemini switch.

---

## Pre-flight (5 min)

### Should I delete my existing data?
**No.** The default-Jina path is bit-for-bit unchanged; your existing 2048d Jina vectors stay valid. The dim guard recognises them on first boot. Only Test 6 mutates data (the marquee provider switch), and it does so safely via the re-embed migration — no data loss possible.

### Cost budget
- Phases 1, 2, 3, 7: $0 (no provider calls beyond what your install already makes).
- Phase 4 (marquee Jina → OpenAI 3-small switch + rollback): ~$0.05 of OpenAI credit for ~5k–50k facts. Trivial.
- Phase 5 (failure modes): $0.

### Time budget
- Minimum smoke (Phases 1–3 + 7): **15 min**.
- Full plan (all 7 phases): **45 min**.

### What to do before starting
1. Confirm the stack is running:
   ```bash
   docker compose ps          # mongodb / weaviate / neo4j / redis healthy
   curl -s http://localhost:8000/api/health | jq .status   # → "healthy"
   ```
2. Confirm the new code is live (run the smoke script):
   ```bash
   BEEVER_API_KEY=$(grep '^BEEVER_API_KEYS=' .env | cut -d= -f2 | cut -d, -f1) \
     python -m scripts.smoke_embedding
   ```
   Expected: **8 / 0 passed/failed**. If any FAIL, stop and diagnose before continuing.
3. (Optional) Take a screenshot of the running Settings page so you have a "before" reference.

---

## Phase 1 · UI layout & navigation (5 min, $0, no mutation)

### Test 1.1 — Tab structure
- **Open**: `http://localhost:5173/settings`.
- **Expected**: Top-level tabs read **Integrations | Channels | Embedding | Agent Models** (4 tabs, was 3).
- **PASS**: Click each tab; right pane changes; URL/state stays consistent.

### Test 1.2 — Embedding tab landing state
- **Click**: `Embedding` tab.
- **Expected**:
  - Card header: "Embedding Model — Jina AI · jina-embeddings-v4 · 2048d · `SAVED`".
  - Three pills: `Multilingual ✓`, `$0.18 / 1M tokens`, `Cloud`.
  - **Step 1 · Choose provider** label + 7 tiles (Jina, OpenAI, Cohere, Voyage, Gemini, Mistral, Ollama). Bedrock + Vertex AI **NOT shown by default**.
  - Jina tile selected (primary border + check icon).
  - **Step 2 · API Key** field showing masked value (e.g., `jina...3Y3T`) with a Replace button.
  - **Advanced** disclosure collapsed.
  - **Test Connection** + **Save Changes** buttons at bottom (Save disabled because no draft change).
  - Last probe timestamp + green check at top of card.
- **PASS**: All elements render, no console errors.

### Test 1.3 — Show advanced providers toggle
- **Click**: "Show advanced providers" link in the top-right of the tile grid.
- **Expected**: Bedrock + Vertex AI tiles appear (muted/disabled, with text "Configurable via env, no UI preset yet"). Clicking them does nothing.
- **PASS**: Toggling hides/shows the two tiles; muted tiles do not fire onClick.

### Test 1.4 — Help drawer
- **Click**: "What's the difference?" link in the card header.
- **Expected**: Right-side drawer slides in with one-paragraph descriptions for each provider.
- **PASS**: Drawer opens, can close via X button.

### Test 1.5 — Agent Models tab unchanged
- **Click**: `Agent Models` tab.
- **Expected**: Quick Presets row (Balanced/Cost Optimized/Quality First/Local First) + filter + per-agent groups (Ingestion Pipeline, Media, Post-Processing, Wiki, QA). NO Embedding card here anymore.
- **PASS**: This tab now contains ONLY agent-model settings.

---

## Phase 2 · Pre-switch warning + Test Connection (5 min, $0, no mutation)

### Test 2.1 — Pre-switch warning fires on tile click
- **Click**: Google Gemini tile.
- **Expected**:
  - Tile gets primary border + check icon.
  - Pills update to `Multilingual ✓ · $0.025 / 1M · Cloud`.
  - **Inline amber warning banner** appears below the tile grid:
    > "Heads up — switching from `jina_ai/jina-embeddings-v4` to `gemini/gemini-embedding-001` will require re-embedding all stored facts. Search will degrade to keyword-only (BM25) for ~5–15 min while the migration runs. Sync is paused during the window. Cost is shown on Save Changes — typically < $1 for OSS-scale data."
  - Save Changes button enabled.
- **PASS**: Warning appears immediately; banner copy is accurate.

### Test 2.2 — Click Jina tile back
- **Click**: Jina AI tile.
- **Expected**: Warning banner disappears (draft now equals saved); Save Changes disabled.
- **PASS**: Warning is reactive to draft state.

### Test 2.3 — Test Connection: failure path (Gemini without API key)
- **Click**: Gemini tile.
- **Click**: Test Connection.
- **Expected**: ~300 ms spinner, then red banner: "Test failed: 401 invalid api key" or "AUTH_MISSING_API_KEY". (Gemini route reads `GEMINI_API_KEY`; you only have `GOOGLE_API_KEY`.)
- **PASS**: Test fails gracefully, error string surfaces, no exception.
- **Cleanup**: leave the draft on Gemini for the next test.

### Test 2.4 — Test Connection: happy path
- **Click**: Replace button on the API Key field.
- **Paste**: your Google API key (the same one in `GOOGLE_API_KEY=AIza...`).
- **Click**: Test Connection.
- **Expected**: green banner: "Test passed — provider returned 768-dim vector in N ms."
- **PASS**: Probe succeeds, dim 768 confirmed.
- **Cleanup**: Click X on the API key field to clear the draft, click Jina tile to revert.

### Test 2.5 — Advanced disclosure
- **Click**: the "▶ Advanced — model name, dimensions, RPM, base URL" disclosure.
- **Expected**: 4 fields appear (Model autocomplete, Dimensions, RPM, API Base override).
- **Modify**: change RPM from 500 to 600.
- **Click**: Save Changes.
- **Expected**: 200 OK, no migration banner (RPM-only change is dim-safe), card refreshes showing RPM=600.
- **PASS**: Cache-bust path works; advanced fields editable.
- **Cleanup**: change RPM back to 500, Save again.

---

## Phase 3 · CLI alternative path (5 min, $0)

### Test 3.1 — `make reembed-dry-run`
- **Run**: `make reembed-dry-run`.
- **Expected**: Output like:
  ```
  reembed: provider=jina_ai model=jina-embeddings-v4 dim=2048 facts=N names=M concurrency=4
  reembed: --dry-run, exiting without changes
  ```
- **PASS**: Counts match what you can verify in Weaviate / Neo4j; nothing mutated.

### Test 3.2 — `./atlas` install script numbered menu
- **Spin up a sandbox** so your real `.env` survives:
  ```bash
  mkdir -p /tmp/atlas-test && cd /tmp/atlas-test
  cp -r /Users/alanyang/Desktop/beever-ai/beever-atlas/{atlas,docker-compose.yml,.env.example} .
  mv .env.example .env
  ./atlas
  ```
- **Expected** at Step 1/4 — Required LLM keys:
  ```
  Choose your embedding provider (default 1: Jina multilingual)

      1) Jina v4              multilingual    ~$0.18/1M tok  [default]
      2) OpenAI 3-large       multilingual    ~$0.13/1M tok
      3) OpenAI 3-small       multilingual    ~$0.02/1M tok  cheapest cloud
      4) Voyage 3-large       multilingual    ~$0.18/1M tok
      5) Cohere multi-v3      multilingual    ~$0.10/1M tok
      6) Gemini emb-004       multilingual    ~$0.025/1M tok reuses GOOGLE_API_KEY
      7) Mistral embed        multilingual    ~$0.10/1M tok
      8) Ollama nomic         English-leaning FREE (local)

      Choice [1]:
  ```
- **Pick 3** (OpenAI small) → it should prompt for `OPENAI_API_KEY`.
- **Cancel** out of the script (Ctrl+C) so it doesn't actually start a docker stack.
- **Verify**: `cat /tmp/atlas-test/.env | grep '^EMBEDDING_'` shows `EMBEDDING_PROVIDER=openai`, `EMBEDDING_MODEL=text-embedding-3-small`, `EMBEDDING_DIMENSIONS=1536`.
- **PASS**: The numbered menu works; choice routes to the right key prompt; .env updated correctly.

---

## Phase 4 · Marquee scenario — Jina → OpenAI 3-small → back (15 min, ~$0.10)

⚠ **This phase actually mutates data.** It uses the re-embed migration to safely transform your stored vectors, then rolls back. No data loss is possible.

### Test 4.1 — Trigger the migration via UI
- **Click**: Embedding tab.
- **Click**: OpenAI tile. Model auto-fills `text-embedding-3-large` — manually change it to `text-embedding-3-small` in the Advanced disclosure (cheapest). Dim auto-fills to `1536`.
- **Click**: Replace on API Key. Paste a real `OPENAI_API_KEY`.
- **Click**: Test Connection.
- **Expected**: green pass, `1536` dim.
- **Click**: Save Changes.
- **Expected**: confirmation modal pops up:
  > **Re-embed migration required**
  > "Switching to openai/text-embedding-3-small changes the vector dimensionality. Atlas must re-embed every stored fact and entity name vector before search will work."
  > Facts to re-embed: N (your fact count)
  > Estimated cost: ~$X.XX (N × 40 tokens × $0.02 / 1M)
  > [ Cancel ] [ Start Migration ]
- **PASS**: Modal renders; cost estimate looks reasonable; runbook link present.

### Test 4.2 — Start the migration
- **Click**: Start Migration.
- **Expected**:
  - Modal closes.
  - Yellow banner appears at the top of the card with **live progress bar**:
    `🔄 Re-embedding in progress · weaviate_facts · 0% · ETA …m`
  - Polls every 2 seconds.
  - Card pills update immediately to OpenAI values.
- **PASS**: Banner appears, progress bar advances, ETA shows.

### Test 4.3 — Search during migration (BM25 fallback)
- **Open** the QA panel or `/api/search` curl. Run any query you remember.
- **Expected**: Results return (not 503), but a small note says **"Semantic ranking temporarily off — embedding migration in progress"**. Results may look different than usual (BM25-only ranking).
- **PASS**: Search responds; degradation is communicated.

### Test 4.4 — Sync rejection during migration
- **Open** any channel page. Click the Sync button.
- **Expected**: HTTP 409 with body `{error: "embedding_migration_in_progress", message: "Sync is paused while an embedding migration runs..."}`.
- **PASS**: Sync correctly refuses with friendly message.

### Test 4.5 — Migration completes
- **Wait** for the yellow banner to disappear (typically 2–10 min depending on fact count).
- **Expected**:
  - Banner gone.
  - Card shows OpenAI · text-embedding-3-small · 1536d.
  - Last probe timestamp updated, green check.
- **Run** a search.
- **Expected**: Full hybrid results return, top-K rankings differ slightly from pre-migration (different embedding space — this is correct, not a bug).
- **PASS**: Migration finishes cleanly, search works.

### Test 4.6 — Rollback to Jina
- **Repeat** Test 4.1–4.5 but pick the Jina AI tile.
- **Expected**: Another migration cycle (~similar duration), card returns to Jina v4 @ 2048d, search works.
- **PASS**: Bidirectional switching works; no data loss.

---

## Phase 5 · Failure modes (10 min, $0)

### Test 5.1 — Save with bad API key
- **Click**: OpenAI tile. **Paste a fake `sk-bogus...` key**. Click Save Changes (skip Test Connection).
- **Expected**: warning modal asks "verify with Test Connection first?" — click Save anyway.
- **Migration starts**, almost immediately fails with 401 from OpenAI.
- **Expected**:
  - Banner turns red: "Migration failed: 401 invalid api key".
  - "Resume Migration" button appears.
- **Cleanup**: Click Cancel, switch back to Jina.
- **PASS**: Failure surfaces clearly; system stays in a recoverable state (checkpoint preserved).

### Test 5.2 — Container restart mid-migration
- **Trigger** a Jina → OpenAI migration (per Test 4.1, 4.2).
- **While the banner says "in progress"**: `docker compose restart beever-atlas` (or kill + restart your uvicorn).
- **Expected**:
  - On boot, dim guard sees `embedding_meta.dim=2048` (last successful state) but `settings.dim=1536` (new) and Weaviate has facts at 2048d. **Refuses to start** with the runbook URL.
  - Logs show the EmbeddingDimensionMismatch error.
- **Recover**: in shell, run `make reembed-resume` to finish the migration.
- **Expected**: resume picks up from the last 500-row checkpoint, completes, atomically updates `embedding_meta` to OpenAI 1536d.
- **Restart** container.
- **Expected**: dim guard now passes, container boots.
- **PASS**: Mid-migration crash is recoverable.
- **Cleanup**: switch back to Jina.

### Test 5.3 — `EMBEDDING_DIM_GUARD=false` override
- **Edit** `.env`: change `EMBEDDING_DIM_GUARD=true` → `EMBEDDING_DIM_GUARD=false`.
- **Restart** backend.
- **Expected**: Even if `embedding_meta.dim != settings.dim`, the container BOOTS with a loud WARN line in the logs.
- **Cleanup**: revert `.env` and restart.
- **PASS**: Override works as documented.

### Test 5.4 — Plaintext-leak audit
- **Save** a real OpenAI API key via the UI.
- **Run**:
  ```bash
  docker compose exec mongodb mongosh beever_atlas --eval \
    'JSON.stringify(db.embedding_secret.findOne())' | grep "sk-"
  ```
- **Expected**: NO match. The DB contains `ciphertext_b64`, `iv_b64`, `tag_b64` — never plaintext.
- **Run**:
  ```bash
  docker compose logs beever-atlas | grep "sk-"
  ```
- **Expected**: NO match (or only false positives from your own commands echoing).
- **PASS**: Encryption at rest verified; no log leakage.

---

## Phase 6 · Persistence & restart (5 min, $0)

### Test 6.1 — Restart with default Jina (no-change path)
- `docker compose restart beever-atlas` (or kill + restart uvicorn).
- **Watch logs for**:
  - One deprecation warn per legacy `JINA_*` var (or none if you've cleaned them).
  - One `embedding_health: probe ok provider=jina_ai dim=2048 latency_ms=N` line.
  - "Application startup complete".
- **Reload** UI Settings → Embedding tab.
- **Expected**: identical state to before restart.
- **PASS**: state is durable.

### Test 6.2 — Reset to factory (optional)
- Delete the override doc:
  ```bash
  docker compose exec mongodb mongosh beever_atlas --eval \
    'db.embedding_settings.deleteOne({_id: "embedding_settings"})'
  docker compose exec mongodb mongosh beever_atlas --eval \
    'db.embedding_secret.deleteOne({_id: "embedding_api_key"})'
  ```
- Restart backend.
- **Expected**: `source: "env"` in the GET response (no DB override). API key not configured (relies on `JINA_API_KEY` env). All other settings revert to env-driven defaults.
- **PASS**: clean reset path works.

---

## Phase 7 · Backend test suite parity (2 min)

Run all the unit + integration tests against your local code to confirm parity:
```bash
source .venv/bin/activate
python -m pytest tests/llm/ tests/api/test_embedding_settings_api.py \
  tests/integration/test_embedding_switching_e2e.py tests/test_config.py \
  tests/services/test_rate_limiter.py -v
```
**Expected**: ~92 tests pass.
**PASS**: full backend coverage green.

---

## Pass / fail summary template

Copy this into your tester notes:

| Phase | Test | Result | Notes |
|---|---|---|---|
| 1.1 | Tab structure | ☐ PASS / ☐ FAIL | |
| 1.2 | Embedding tab landing | ☐ PASS / ☐ FAIL | |
| 1.3 | Show advanced providers | ☐ PASS / ☐ FAIL | |
| 1.4 | Help drawer | ☐ PASS / ☐ FAIL | |
| 1.5 | Agent Models tab unchanged | ☐ PASS / ☐ FAIL | |
| 2.1 | Pre-switch warning fires | ☐ PASS / ☐ FAIL | |
| 2.2 | Pre-switch warning reverts | ☐ PASS / ☐ FAIL | |
| 2.3 | Test Connection failure | ☐ PASS / ☐ FAIL | |
| 2.4 | Test Connection happy | ☐ PASS / ☐ FAIL | |
| 2.5 | Advanced disclosure + RPM save | ☐ PASS / ☐ FAIL | |
| 3.1 | `make reembed-dry-run` | ☐ PASS / ☐ FAIL | |
| 3.2 | `./atlas` numbered menu | ☐ PASS / ☐ FAIL | |
| 4.1 | Migration confirmation modal | ☐ PASS / ☐ FAIL | |
| 4.2 | Migration starts + progress | ☐ PASS / ☐ FAIL | |
| 4.3 | Search during migration | ☐ PASS / ☐ FAIL | |
| 4.4 | Sync rejection | ☐ PASS / ☐ FAIL | |
| 4.5 | Migration completes | ☐ PASS / ☐ FAIL | |
| 4.6 | Rollback to Jina | ☐ PASS / ☐ FAIL | |
| 5.1 | Bad API key | ☐ PASS / ☐ FAIL | |
| 5.2 | Mid-migration restart + resume | ☐ PASS / ☐ FAIL | |
| 5.3 | `EMBEDDING_DIM_GUARD=false` | ☐ PASS / ☐ FAIL | |
| 5.4 | Plaintext-leak audit | ☐ PASS / ☐ FAIL | |
| 6.1 | Restart no-change path | ☐ PASS / ☐ FAIL | |
| 6.2 | Reset to factory | ☐ PASS / ☐ FAIL | |
| 7 | Backend test suite | ☐ PASS / ☐ FAIL | |

---

## What to do if a test fails

1. **Take a screenshot** of the failing UI state, or
2. **Capture the response/log**:
   ```bash
   docker compose logs --tail=100 beever-atlas
   ```
3. **Check the runbook** at `docs/runbooks/embedding-migration.md` for known recovery paths (mid-migration crash, dim guard refusal, etc.).
4. **Diagnose**: most failures will fall into one of these buckets:
   - 401/403 from provider → API key wrong or missing.
   - Dim guard refusal → need to run `make reembed-resume` or revert env to persisted dim.
   - 503 search during migration → expected, BM25 fallback fires.
   - 409 sync during migration → expected.
5. **Report**: paste screenshot/log + which test number into the PR thread.

---

## Recommended order if you only have 15 min

If pressed for time, do this minimum sequence (skip Phase 4 / 5):
1. **Pre-flight** smoke (5 min)
2. **Test 1.1, 1.2, 1.5** — UI baseline (3 min)
3. **Test 2.1, 2.2, 2.4** — warning + Test Connection happy (3 min)
4. **Test 6.1** — restart consistency (2 min)
5. **Test 3.1** — `make reembed-dry-run` to confirm CLI works (2 min)

That's enough to convince yourself the feature works on the no-change + read-only paths. The marquee Phase 4 (real provider switch) is what proves the whole feature; do it when you have the cost budget.
