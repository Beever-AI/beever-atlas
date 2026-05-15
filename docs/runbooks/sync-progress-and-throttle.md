# Sync Progress + LLM Throttle Runbook

This runbook covers the operator-facing surfaces shipped with the
`sync-pipeline-feedback-and-auto-wiki` change. It is the canonical
reference for tuning, observing, and rolling back the throttle, the
maintainer debounce, the auto-overview subscriber, and the new
`/sync/status` payload.

## What changed (in one paragraph)

The sync pipeline used to mark every claimed row as failed when *any*
sub-batch hit a Gemini 429, then bursts past the provider's RPM limit
to trigger more 429s, then over-rewrite the wiki on every fact event,
then never auto-build the channel-overview wiki at all. After this
change: per-sub-batch failure attribution, a token-bucket throttle in
front of every LiteLLM call, debounced wiki rewrites, auto-overview
on first sync, smoothed ETA, and a phased status payload the UI
reflects in real time. End-to-end target: 700-message channel from
**~30 minutes → < 5 minutes**.

## New environment variables

All variables are optional with safe defaults. Set them in `.env` (or
the equivalent secrets store for your deploy).

| Variable | Default | Purpose |
|---|---|---|
| `LLM_RPM_OVERRIDE_GEMINI` | `10` | Gemini RPM ceiling (free tier). Bump to `360` for paid Gemini, higher for enterprise quota. |
| `LLM_TPM_OVERRIDE_GEMINI` | `250000` | Gemini tokens-per-minute ceiling. Bump alongside RPM if you upgrade tier. |
| `LLM_RPM_OVERRIDE_OPENAI` | `500` | OpenAI tier-1 RPM. |
| `LLM_TPM_OVERRIDE_OPENAI` | `200000` | OpenAI tier-1 TPM. |
| `LLM_RPM_OVERRIDE_<PROVIDER>` | (provider default) | Same pattern for Voyage, Cohere, Mistral, Jina, Ollama. |
| `LLM_BACKOFF_COOLDOWN_SECONDS` | `60` | After an observed 429 the bucket fill rate is halved for this many seconds. Overlapping 429s do not stack. |
| `WIKI_MAINTAINER_DEBOUNCE_SECONDS` | `60` | Per-page rewrites are coalesced inside this window. A burst of 30 events touching one page collapses to one rewrite. |
| `AUTO_OVERVIEW_WIKI` | `true` (fresh install), `false` (upgrade) | Auto-generate the channel-overview wiki on first sync. The default flips automatically based on whether `wiki_pages` has any rows at startup; an explicit env value always wins. |
| `OVERVIEW_MIN_FACTS` | `5` | Minimum extracted facts before the auto-overview will fire. Tiny channels still wait for a manual click. |
| `WIKI_DEFAULT_LANGUAGE` | `en` | Default language for auto-generated overview wikis. Per-channel policy `wiki.default_language` overrides. |
| `DECOUPLE_EXTRACTION` | `true` | Background-worker extraction. Keep on. The sync trigger will not return quickly if turned off. |

### When to tune

- **Free-tier Gemini operator** — accept the conservative defaults.
  Performance will be capped by Gemini's 10 RPM. Enable Ollama for
  embedding to bypass the constraint on that side.
- **Paid Gemini operator** — set `LLM_RPM_OVERRIDE_GEMINI=360` and
  `LLM_TPM_OVERRIDE_GEMINI=4000000` (the published "tier 2" numbers
  as of 2025-Q1). Re-check the console — the limits change.
- **High-throughput install** — bump every provider's overrides to
  match your contract. Watch the throttle metrics endpoint to confirm
  blocked-call counts trend toward zero.
- **Aggressive wiki maintenance** (operators who want every change
  reflected immediately) — drop `WIKI_MAINTAINER_DEBOUNCE_SECONDS`
  to e.g. `10`. Trade-off: more LLM calls per fact event.
- **Existing install onboarding** — leave `AUTO_OVERVIEW_WIKI` at the
  fresh-install default if you want auto-generation enabled, or set
  `AUTO_OVERVIEW_WIKI=false` to keep the manual workflow.

## Observability endpoints

| Endpoint | Auth | What it returns |
|---|---|---|
| `GET /api/admin/llm-throttle/metrics` | admin token | Per-provider snapshot: `{rpm_limit, tpm_limit, rpm_used_60s, tpm_used_60s, blocked_calls_60s, recent_429s_60s}` |
| `GET /api/admin/extraction-worker/metrics` | admin token | Worker tick history, queue depth per channel, breaker state |
| `GET /api/admin/wiki-maintainer/metrics` | admin token | Maintainer rewrite counts (5/15/60 min), recent failures |
| `GET /api/channels/{id}/sync/status` | user token | Per-channel phase view + smoothed ETA + recent_events |

### What healthy looks like

- `recent_429s_60s` for every provider is **zero or single-digit**.
  If you see double digits, the throttle is too loose for the
  provider's tier — drop the `LLM_RPM_OVERRIDE_<PROVIDER>` value.
- `apply_update_count_60min` for the wiki maintainer is **<60** for a
  freshly synced 700-message channel. Higher means the debounce
  window is too short or the deterministic routing is touching too
  many pages per fact.
- Worker tick history shows `succeeded > 0 AND failed >= 0`. The
  pre-fix pattern of `succeeded=0 failed=200` should never appear —
  if it does, the per-sub-batch attribution path is mis-firing
  (verify `BatchResult.batch_breakdowns[i].keys` is populated).
- `/sync/status` `phases[]` array progresses
  `[done, in_flight, in_flight, pending] →
   [done, done, in_flight, pending] →
   [done, done, done, in_flight] →
   [done, done, done, done]`
  in temporal order. Stalled phase = root-cause investigation needed.

## The activity feed

`/sync/status` also returns `recent_events` — the last 10 pipeline
events for that channel. Stages emit:

```
fetch              · "Claimed N pending rows"
preprocess         · "Retained N · K media · L coref · M threads · O links"
extract_facts      · "Extracted N facts (avg quality 0.74)"
extract_entities   · "Extracted N entities"
embed              · "Embedded N facts"
persist            · "Saved N facts, M entities, K rels"
wiki_maintenance   · "Rewrote page entity:lenovo-pgx (3 facts integrated)"
overview_wiki      · "Generating overview wiki for channel C123 (en)"
```

Labels are integer-derived only — they never include channel content
or fact text. Safe to render verbatim in the UI.

## Phased progress card

The UI's `PhasedProgressCard` reads `phases[]` and renders one row
per phase with state icon (✓ done · ⟳ in-flight · ○ pending ·
⏭ skipped · ✗ failed) plus a per-row progress fraction where
applicable. The smoothed ETA (5-min EWMA) renders next to the active
phase; "Calculating…" appears when the window has fewer than 3
samples — small syncs often complete before the ETA stabilises and
that's fine.

The retrying-vs-failed distinction:

- `extraction_status="failed" AND next_attempt_at > now` → **amber
  "Retrying — N rows"** chip; rows will be re-claimed when the backoff
  expires.
- `extraction_status="failed" AND attempt_count >= max_retries` →
  **red "Abandoned — N rows"** chip; clickable to drill into the
  failed-batch panel for manual recovery.

## Auto-overview wiki

Subscribed to `ExtractionWorker.on_extraction_done`. Five gates run
in order before a generation job is enqueued:

1. `AUTO_OVERVIEW_WIKI` feature flag is true.
2. No auto-generate task already in-flight for the channel.
3. Channel extraction is fully complete (`pending+extracting=0`).
4. Channel has at least `OVERVIEW_MIN_FACTS` extracted facts (default 5).
5. No overview row already exists for the channel.

Idempotent — duplicate events while a job is in flight no-op. The
generation path is the same one the manual "Generate" button uses; no
parallel implementation.

## Rollback procedures

Each phase ships independently and is reverted independently. Behavior
after each rollback:

| Commit | What rollback restores |
|---|---|
| `1741c6c` per-sub-batch attribution | All-or-nothing finalization returns. Pipeline still works; throughput drops back to ~14 msgs/min on a 711-msg channel under rate-limit storms. |
| `2223117` LLM throttle | Throttle disabled; 429s return at high volumes. Legacy `aiolimiter` in `batch_processor` continues to gate the genai-bound stages. |
| `b348d2d` maintainer debounce | Synchronous per-event rewrites return; ~400 LLM calls per 700-msg sync. |
| `e363641` auto-overview | Manual Generate button is the only path; "No Wiki Yet" stays until clicked. Set `AUTO_OVERVIEW_WIKI=false` for soft-rollback without reverting code. |
| `944d805` API extension + ETA | Status payload reverts to legacy fields only; UI gracefully falls back to `ExtractionWorkerPanel` rendering. |
| `79ee613` UI phased card | Frontend only; revert to redeploy old bundle. |
| `54e779b` simulated test harness | Test-only revert; no production behavior change. |
| `fbf5958` review fixes | Restores the four findings (bucket race, oversized-request hang, missing wiki phase counters, missing genai-bypass note). Strictly worse — do not roll back without replacing. |

## Known follow-ups

- **GenAI-side throttle.** `LLMThrottle` wraps `litellm.acompletion`
  and `litellm.aembedding` only. The Google GenAI SDK calls used by
  the ADK ingestion agents (`fact_extractor`, `entity_extractor`,
  `coreference_resolver`, etc.) and the wiki maintainer's
  `client.aio.models.generate_content` calls bypass it. They are
  still gated by the legacy `batch_processor._get_limiter`
  aiolimiter and the circuit breaker — not unprotected — but the
  spec's "every outbound LLM call goes through one throttle" promise
  is not yet met. Tracked in the architect's R1 finding; follow-up
  options: (a) wrap each call site in a `dispatch_genai_completion`
  helper, or (b) install the throttle as an ADK-layer interceptor on
  `LlmAgent`. Option (b) is the right architectural answer.
- **Per-channel maintainer counters.** The `wiki_maintenance.done`
  and `total` fields in `/sync/status` come from the maintainer's
  global counters today (rough proxy). Per-channel tracking is
  available in `wiki_pages` but requires a Mongo round-trip per
  status poll; defer until operators ask for the exact figure.
- **Smoothed ETA stall detection.** The current implementation drops
  zero-throughput ticks before EWMA-ing. A sustained rate-limit
  storm produces a too-optimistic ETA. Consider blending in zero
  ticks at low weight to capture stall states.

## Quick triage

- **Sync stuck for >10 minutes** — check `/api/admin/extraction-worker/metrics`
  for `breaker_state`. Open breaker = upstream provider outage; wait or
  flip `EMBEDDING_PROVIDER` to a working alternative.
- **Wiki not auto-generating** — check `phases[*]` in `/sync/status`
  for `overview_wiki.state`. `skipped` = feature flag off; `pending`
  with `done` extraction = below `OVERVIEW_MIN_FACTS` threshold.
- **Phased card not rendering** — old backend payload missing
  `phases[]`. Check the `/sync/status` response for the field;
  legacy `ExtractionWorkerPanel` should render in the meantime.
- **Sudden 429 storm** — check `LLM_RPM_OVERRIDE_<PROVIDER>` against
  the provider console. Lower it to match published quota with a
  20% safety margin.
