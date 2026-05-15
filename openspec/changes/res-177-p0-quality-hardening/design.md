## Context

This change closes the seven P0 sub-issues of Linear RES-177 that remain open
after PR #42 shipped the H1/H2/H3/H4/M1/M6 security fixes. The umbrella
milestone is **v1.0 OSS Launch**. Each sub-issue was authored by the
`SECURITY_REVIEW_2026-04-15` audit; this doc consolidates the implementation
approach because the seven fixes share one branch, one PR, and one shared
concern: making the repo safe to be read and deployed by strangers after OSS.

Constraints:

- Current baseline: backend `uv run pytest` = 1,140 pass / 2 pre-existing fail
  (`test_qa_chat_overhaul` — unrelated) / 25 skipped; bot `npm test` = 116 pass
  / 0 fail. Must not regress.
- Web tests are currently RED on `main` (RES-214) — this change must make them
  GREEN, not just "equally broken".
- Root main branch has moved since audit; two items regressed
  (`bridge.ts` +633 LoC, `test_graph_store_contract.py` +1 unguarded import).
  All location references below reflect the current `main` tree.
- No user-facing API surface changes except `/api/health` (see decision D5).

## Goals / Non-Goals

**Goals:**

- Close all seven RES-177 P0 sub-issues with testable success criteria.
- Ship one atomic branch with per-ticket commits so each Linear issue can be
  moved to Done with a clear commit pointer.
- Add just enough CI enforcement that a future regression on any of these
  seven items fails a check run rather than landing on `main`.
- Leave the refactored `bridge.ts` behaviourally identical — same routes,
  same response envelopes, same platform-error classification, same tokens
  attached to outbound fetches.

**Non-Goals:**

- Not raising coverage to 80% on every security-sensitive module (RES-208
  Q3.3 enumerates 9 modules at 0–15%). Scope kept to: landing the ratchet at
  50% and adding tests for the two most critical (`share_store`,
  `quality_gates`). Remaining coverage work is tracked as a follow-up.
- Not rewriting `Makefile`, `deploy.yml` smoke harness, or Nebula nightly
  service ordering (RES-206 Q1.6 / Q1.7 / Q1.9). Out-of-scope stretch items
  land as follow-up tickets.
- Not introducing Renovate. Dependabot extended config is sufficient for
  digest + chat-SDK grouping.
- Not replacing `chat` SDK family with `@slack/bolt` / `discord.js` etc.
  (RES-195 recommendation #3). That's a large migration tracked separately;
  scope here is pin + verify.

## Decisions

### D1 — One branch, seven commits, one PR

Alternatives: seven sibling branches (one per ticket) merged independently;
rejected because six of the seven fixes either touch CI or Docker configs
that must land together to avoid transient CI failures. Commit-per-ticket on
one branch keeps PR review legible and each Linear close pinned to a single
SHA.

### D2 — `classifyPlatformError` canonical location

Move the canonical (hardened) implementation to
`bot/src/bridge/platformError.ts`. `bot/src/bridge-classifier.ts` is
**deleted** (not a re-export shim) — keeping the file tempts future edits to
drift it again. Both test files
(`bot/src/bridge-error-classifier.test.ts` and
`bot/src/bridge-classifier.test.ts`) are updated to import from the new
canonical module. If the tests are redundant after the merge, they are
consolidated into a single test file.

Alternatives: keep `bridge-classifier.ts` as a `export *` shim — rejected,
since reviewers and IDE-goto-definition would land in the shim and still
need one extra hop.

### D3 — Route table for `registerBridgeRoutes`

Replace the 201-line `if/else` cascade with a table:

```ts
const ROUTE_TABLE: Array<{ method: string; pattern: RegExp; handler: Handler }> = [
  { method: "POST", pattern: /^\/bridge\/send$/, handler: handleSend },
  // …
];
```

Each handler is wrapped with `withPlatformError(handler)`. The three-axis
duplication (legacy + connection-scoped + platform-prefixed) is collapsed by
having the pattern resolver normalise the matched URL into a canonical form
before dispatching.

### D4 — Logger is `bot/src/logger.ts`, not pino

60-line wrapper around `console.*` with level gates driven by
`LOG_LEVEL` env. Reasons: (a) zero new deps — keeps bot install small; (b)
no existing structured-log consumer — pino's JSON output would be wasted;
(c) easy to swap later. The bar is "can silence debug in prod" + "one
module touches stdout/stderr so a future pino swap is one-line".

### D5 — `/api/health` never raises

Today `infra/health.py:69` raises `redis.exceptions.ConnectionError` when
Redis is down. The fix wraps each probe in its own typed `try/except`,
aggregates into `{status: "healthy"|"degraded"|"unhealthy", failing: [...]}`,
always returns HTTP 200. **This is a behavioural change** visible to health
checkers — Kubernetes readiness probes that currently interpret 5xx as "not
ready" will now always see 200 and must switch to reading the JSON status
field. Updated in ops README.

Alternatives: return HTTP 503 on degraded — rejected because AWS ALB and
other L7 checkers treat 503 as fail-closed-remove-from-LB; we want degraded
to be an observable state, not an auto-remove.

### D6 — Docker digest pinning via Dependabot + fall-back manual pin

Dependabot 2 supports digest updates for Docker via `package-ecosystem:
docker` with `rebase-strategy: auto`. Initial digests captured by running
`docker buildx imagetools inspect <image>:<tag>` on the build host and
committed. Dependabot then opens PRs when upstream repoints the tag.

Rejected: Renovate — would require a new bot + config file; for the current
update volume (two Python, two Node, 5 service images), Dependabot is
sufficient.

### D7 — CodeQL workflow: fail + upload (do not delete)

Drop both `continue-on-error: true` and `upload: never`. Keep the workflow.
Reason: without CodeQL we have no JavaScript/TypeScript SAST at all. The
current configuration is strictly worse than nothing because it advertises
coverage that doesn't exist.

### D8 — Coverage ratchet starts at 50%, not today's 51%

`pytest --cov-fail-under=50`. Chose 50% over 51% to leave room for landing
silent-except-pass fixes (Q3.5) which typically **drop** line coverage by
1–2 pts because they log a previously-untested branch. Once this change
merges, a follow-up issue raises the floor to 55% and each quarter thereafter.

### D9 — `.gitignore` drops `openspec/`

Recommended fix from RES-213 Q8.1. The alternative ("untrack existing files
and document explicit exceptions") was rejected because the project is
already actively working with OpenSpec changes (this doc is one of them)
and invisibility in `git status` has caused real confusion.

## Risks / Trade-offs

- **Risk:** Digest-pinning delays security updates for base images until a
  Dependabot PR lands. → **Mitigation:** Dependabot runs daily; auto-merge
  enabled on the digest-update workflow for the Docker ecosystem only after
  CI green.
- **Risk:** Decomposing `bridge.ts` into route modules changes import paths
  consumed by `bot/src/index.ts` and test files. → **Mitigation:** keep the
  top-level `bot/src/bridge.ts` as a re-export shim that still exposes
  `registerBridgeRoutes` and `classifyPlatformError` for backward compat
  within the bot package only. Delete the shim in a follow-up once all
  internal consumers are migrated.
- **Risk:** CodeQL hard-fail surfaces a backlog of alerts that blocks the PR.
  → **Mitigation:** run CodeQL locally first; triage and either fix or
  add `# codeql[suppress]` with a tracking ticket before turning hard-fail on.
- **Risk:** `/api/health` never-raise contract changes healthchecker
  semantics. → **Mitigation:** document in README + release notes;
  smoke-test against the deployed EIP before prod cutover.
- **Trade-off:** Scope cut on RES-208 coverage goal (not hitting 80% on every
  security-sensitive module) keeps this PR reviewable. Explicit follow-up
  ticket linked in Q3 close-out comment.
- **Trade-off:** One big PR has larger blast radius than seven small ones.
  Acceptable because per-commit atomicity means `git revert` per ticket
  remains possible, and the CI gates landing in this PR protect the whole
  bundle.

## Migration Plan

1. Create branch `feature/res-177-p0-quality-hardening` (done).
2. Phase in commits in the order below; each phase is independently
   revertible. Each phase ends with the targeted Linear sub-issue moved to
   **Done** with a comment citing the commit SHA.
   - Phase 1 — RES-213 (Q8): docs / env / hygiene. Lowest blast radius.
   - Phase 2 — RES-206 (Q1): CI gates. Must land before later phases so
     their regressions fail CI.
   - Phase 3 — RES-194 (H5): Docker digest pins.
   - Phase 4 — RES-195 (H6): Chat SDK pinning.
   - Phase 5 — RES-214 (Q9): Web test harness.
   - Phase 6 — RES-208 (Q3): Backend test baseline.
   - Phase 7 — RES-209 (Q4): bridge.ts decomposition. Highest risk, landed
     last with full test safety net from earlier phases.
3. Final commit: `docs(security): add RES-177 P0 follow-up close-out notes`
   linking each closed ticket to its commit.
4. PR to `main`. Mark RES-177 umbrella as "all P0s closed" once merged.

**Rollback:** `git revert` per phase commit. CI contract added in Phase 2
prevents a phase-specific rollback from reintroducing the fixed regression
silently (e.g. re-raise on `/api/health` would fail a new health-shape test).

## Open Questions

- **Q:** Do we want Renovate eventually, or is Dependabot our long-term
  answer? → Decision deferred to a separate ticket; this PR uses Dependabot.
- **Q:** Should the CodeQL workflow run on every PR or only `main` + nightly?
  → Today it runs on both; keeping the current trigger set. Revisit if PR
  runtime becomes a bottleneck.
- **Q:** Coverage ratchet cadence — monthly or per-PR high-watermark?
  → Monthly for now; high-watermark has a well-known sawtooth problem on
  refactors.
