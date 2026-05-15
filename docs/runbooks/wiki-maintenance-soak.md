# Wiki Maintainer auto-mode soak runbook

This runbook covers §22 of the `oss-redesign-production-wiring` change —
the staging soak that gates flipping `WIKI_MAINTENANCE_MODE=auto` to
default ON in production.

The unit + integration tests verify the structural correctness of
each layer in isolation. The soak verifies the integrated behaviour
in a live environment over a 2-week window, with real LLM cost.

## §22.1 E2E — full ingest → extraction → wiki refresh

Run in staging with a fresh test channel. Verifies the redesign's
"compounding LLM Wiki" promise end-to-end.

```bash
# 1. Create a test channel + register a push source for it.
curl -X POST https://atlas.staging/api/admin/sources \
  -H "X-Admin-Token: $BEEVER_ADMIN_TOKEN" \
  -d '{"source_id": "soak-test", "allowed_channels_pattern": "soak-*"}'

# 2. Push 100 synthetic messages signed with the returned secret.
# (See docs/integrations/push-sources.md for the signing recipe.)

# 3. Watch extraction drain.
watch -n 5 "curl -s https://atlas.staging/api/channels/soak-channel-1/extraction-status \
  -H 'Authorization: Bearer $BEARER' | jq"

# 4. After counts.pending=0, fetch one wiki page and assert content
#    is NOT the legacy placeholder.
PAGE=$(curl -s "https://atlas.staging/api/channels/soak-channel-1/wiki/page/topic:auth" \
  -H "Authorization: Bearer $BEARER")
echo "$PAGE" | jq -r '.sections[].content_md' | grep -q "New facts integrated:" \
  && echo "FAIL: placeholder still in content_md" \
  || echo "PASS: real LLM content"
```

Pass criteria:
- Within ~60s of the last push, all 100 messages reach `extraction_status="done"`.
- The maintainer fires (visible in logs as `wiki_maintainer.on_extraction_done`).
- Affected wiki pages contain natural-language content, NOT the literal
  string `"New facts integrated: <ids>"`.

## §22.2 E2E — admin UI → signed push round-trip

1. Navigate to `/admin/sources` in the staging dashboard.
2. Register a new source (`soak-source-2`).
3. Copy the secret from the modal.
4. Use it to sign + POST 50 events to the staging API (see
   `docs/integrations/push-sources.md`).
5. Open the wiki for the target channel; expect pages to refresh
   within seconds (with `wiki.maintenance_mode=auto`).

Pass criteria:
- Events appear in `channel_messages` with the new `source_id`.
- The maintainer rewrites affected pages (visible in
  `GET /api/admin/extraction-worker/metrics` `claim_rate` rising then
  falling).
- The wiki UI reflects new content.

## §22.3 E2E — lint with intentional orphans

1. Pick a channel with an existing wiki.
2. Manually delete a topic cluster from Weaviate (or seed a stale
   `wiki_pages` row whose `cluster_id` no longer exists). Document the
   exact one-shot SQL/CLI used.
3. POST `/api/channels/{id}/wiki/lint`.
4. Expect a finding with `category="orphan"` for the orphaned page.
5. Click the finding in the UI → navigates to the affected page.

## §22.4-§22.7 Drift A/B comparator soak (the actual gate)

This is the longest-running step. Run it on **at least 3 real channels**
for a continuous 2-week window. The comparator emits one
`wiki_drift_report` log line per `apply_update` AND persists each report
to the `wiki_drift_reports` Mongo collection (TTL=30 days). The admin
dashboard reads the persisted collection — the log lines are kept as
defense-in-depth.

### Setup

1. Pick 3 channels with active conversation: ideally one short
   (low traffic), one medium, one busy.
2. Set `WIKI_DRIFT_AB=true` in their staging env.
3. Optional: tune `WIKI_DRIFT_AB_RATE_LIMIT_SECONDS` (default `60`) —
   raise to `300` if soak LLM cost is too high; drop to `30` if data
   density is too low for confident percentile estimation. The rate
   limit is per `(channel_id, page_id)` pair and applies in addition to
   the maintainer's existing per-page semaphore.
4. Set `wiki.maintenance_mode=auto` per-channel (via the
   ChannelSettingsTab toggle).
5. The comparator wiring is now LIVE in `WikiMaintainer.apply_update`'s
   success path — no manual hookup required (close-the-soak-loop §1).

### Monitoring — primary path

Open the admin dashboard:

```
https://atlas.staging/admin/wiki-drift
```

The page polls `GET /api/admin/wiki-drift/summary?days=14` every 5
minutes and renders:

- Top banner: green `PASSING — soak threshold met across N channels` or
  red `FAILING — drift X.XX exceeds threshold on M channels`.
- Yellow `data_fresh=false` warning when the most recent drift report
  per channel is more than 1 hour old (likely cause: `WIKI_DRIFT_AB`
  was flipped off mid-soak, or the comparator stalled).
- Per-channel rows with `page_count`, `p50_median`, `p95_median`,
  relative `last_run` time, and the threshold ✓/✗ marker.

`VITE_BEEVER_ADMIN_TOKEN` must be set in `web/.env.local` for the
dashboard to authenticate against `/api/admin/wiki-drift/summary`.

### How the dashboard works

The aggregation runs on the API layer over the persisted
`wiki_drift_reports` collection (NOT log files). Each per-channel row
shows the **median of medians** — i.e. for each `apply_update` the
comparator records `levenshtein_section_p50` (one number per page); the
dashboard takes the median of those numbers per channel over the window.
That smooths over per-page noise without hiding a sustained shift in any
direction. The `data_fresh` indicator reads `MAX(ts)` per channel; ANY
channel falling behind by more than an hour flips the dashboard-wide
freshness warning regardless of overall pass status.

### Monitoring — fallback (no dashboard access)

If you don't have admin-token access to the staging dashboard, tail
logs and aggregate manually:

```bash
# Aggregate per-channel medians + p95 from the structured log lines.
# Adjust the log path to your environment. The dashboard is the
# canonical view; this is for ops without admin-token access.
grep -h "event=wiki_drift_report" /var/log/beever-atlas/*.log \
  | python -c "
import sys, statistics
from collections import defaultdict
sections = defaultdict(list)
for line in sys.stdin:
    parts = dict(p.split('=') for p in line.split() if '=' in p)
    if 'levenshtein_section_p50' in parts:
        sections[parts.get('channel_id', '?')].append(float(parts['levenshtein_section_p50']))
for ch, vals in sections.items():
    print(f'{ch}: median={statistics.median(vals):.3f} '
          f'p95={statistics.quantiles(vals, n=20)[-1]:.3f} samples={len(vals)}')
"
```

### Pass criterion

Median Levenshtein < 0.15 AND p95 < 0.30 across all sections, sustained
for 2 weeks across all 3 channels. Document the daily summary.

### If pass

- Open a PR flipping the env-default `WIKI_MAINTENANCE_MODE` from
  `"manual"` to `"auto"`.
- Tag the responsible operator + a code reviewer.
- Roll forward to production with the new default after staging burn-in
  passes.

### If fail (drift exceeds threshold on any channel)

- Do NOT flip the default.
- Capture sample drifts (the structured log line includes
  `sample_section_diffs` for the worst sections).
- Iterate on the `_render_apply_update_prompt` template OR the
  `plan_updates` routing (the most common cause of drift is
  the maintainer routing facts to the wrong page; second most common
  is prompt template not preserving voice).
- Re-run the soak on the iterated prompt for another 2 weeks.

## Worked example — "PASSING after 2 weeks"

After 14 days of soak across 3 channels, the dashboard reads:

| Channel | Reports | p50 median | p95 median | Last run | Threshold |
|---|---:|---:|---:|---|:---:|
| `C-design`  | 412 | 0.082 | 0.198 | 2m ago  | ✓ |
| `C-eng`     | 631 | 0.094 | 0.221 | 4m ago  | ✓ |
| `C-product` | 287 | 0.074 | 0.187 | 8m ago  | ✓ |

Banner reads `PASSING — soak threshold met across 3 channels`. At this
point:

1. Capture the dashboard screenshot — `${BEEVER_DOCS_URL}/runbooks/wiki-soak-pass-2026Q2.png`
   (placeholder — substitute the actual artifact path).
2. Open a PR flipping the env-default for `WIKI_MAINTENANCE_MODE` from
   `"manual"` to `"auto"` in `infra/config.py`.
3. Tag the responsible operator + a code reviewer; reference this
   runbook section for the soak evidence.

## Related — push-source integrations

Operators integrating new push sources (OpenClaw, Hermes, etc.) should
keep an eye on `/admin/wiki-drift` during the first week of the
integration's soak. A new ingestion path can introduce subtle source-
language or fact-shape changes that may show up as elevated p95 even
when p50 stays clean.

- See `docs/integrations/openclaw.md` for the OpenClaw HMAC-signed
  push setup. Watch the dashboard during the first sync to confirm
  `pass_criterion_met=true` once their channel accumulates ≥30 drift
  reports.
- See `docs/integrations/hermes.md` for the Hermes push integration.
  Same drift-watch contract applies.

## Scheduled re-runs

After the initial soak, schedule a re-run quarterly so prompt-shift
on the underlying LLM doesn't silently drift back over the threshold.
The drift comparator stays in the code; flip `WIKI_DRIFT_AB=true` for
the soak window each quarter and watch `/admin/wiki-drift` again.

## §22.8 Per-kind drift soak — wiki-llm-native-redesign

The LLM-native redesign (change ID `wiki-llm-native-redesign`) gates
flipping its `WIKI_LLM_NATIVE_REDESIGN` flag from default-OFF to
default-ON in `.env.example` on a 7-day per-kind drift soak. The
mechanics mirror §22.4–§22.7 but the dashboard now exposes per-kind
medians + p95s so the operator can spot a degraded prompt for one
specific kind (e.g. the `decisions` page kind regressing while
`topic` stays clean).

### Setup

1. Pick a real Mattermost (or other adapter) channel with at least
   25 wiki pages spanning multiple kinds. The dogfood channel from
   the change's redesign branch is the canonical reference.
2. Set in `.env`:
   ```
   WIKI_LLM_NATIVE_REDESIGN=true
   WIKI_DRIFT_AB=true
   WIKI_DRIFT_AB_PER_KIND_SAMPLE_RATE=0.05
   ```
3. Restart `beever-atlas` so the new settings load.

### Per-kind facets on `/admin/wiki-drift`

The summary endpoint (`GET /api/admin/wiki-drift/summary`) now returns
one row per channel with both the channel-level totals AND a `per_kind`
map keyed by kind. Each per-kind entry carries
`levenshtein_section_p50_median`, `levenshtein_section_p95_median`,
`page_count`, and `last_run_ts`. A worked excerpt:

```json
{
  "channel_id": "C-ENG",
  "page_count": 28,
  "levenshtein_section_p50_median": 0.08,
  "levenshtein_section_p95_median": 0.21,
  "per_kind": {
    "topic":         { "page_count": 18, "p50": 0.07, "p95": 0.18 },
    "entity":        { "page_count": 5,  "p50": 0.09, "p95": 0.22 },
    "decisions":     { "page_count": 3,  "p50": 0.12, "p95": 0.31 },
    "faq":           { "page_count": 1,  "p50": 0.05, "p95": 0.14 },
    "action_items":  { "page_count": 1,  "p50": 0.04, "p95": 0.11 }
  }
}
```

### Pass criterion

The redesign soak passes when, **across all five page kinds**:

- median Levenshtein < 0.15 — operator-visible drift is bounded;
- p95 < 0.30 — no kind has a long tail of high-divergence pages.

If any single kind misses, dig in before flipping the default. The
common offender is `decisions` (rare facts, high stakes) — sometimes
its prompt needs tightening before the rollout.

### Decision criteria for flipping the default

Open the PR per §9.3 only when:

1. The `/admin/wiki-drift` dashboard has had `pass_criterion_met=true`
   per-kind for ≥7 consecutive days;
2. No `wiki_kind_schema_validation_failed` warnings in the last 24
   hours of structured logs (each warning is a 2-strike validation
   miss — they should be vanishingly rare on a healthy prompt set);
3. Migration script `scripts/migrate_wiki_pages_to_slug_identity.py`
   has been run with `--dry-run` on the production cluster and the
   change set is reviewed.

If all three hold, the PR flipping `WIKI_LLM_NATIVE_REDESIGN=true` in
`.env.example` is safe.
