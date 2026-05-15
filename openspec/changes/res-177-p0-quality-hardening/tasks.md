## 0. Baseline

- [ ] 0.1 Confirm branch `feature/res-177-p0-quality-hardening` is current and tracks `main`.
- [ ] 0.2 Capture baselines: `uv run pytest` pass/fail counts, `cd bot && npm test` counts, `cd web && npm test -- --run` pass/fail counts, `uv run ruff check .` count, `wc -l bot/src/bridge.ts`.
- [ ] 0.3 Move `RES-213, RES-206, RES-194, RES-195, RES-214, RES-208, RES-209` to **In Progress** on Linear with a comment linking this OpenSpec change.

## 1. Phase 1 — RES-213 (Q8) Docs, .env.example, repo hygiene

- [ ] 1.1 Drop `openspec/` entry from `.gitignore`; verify `git status` then surfaces `openspec/changes/res-177-p0-quality-hardening/` as untracked.
- [ ] 1.2 Rewrite `.env.example`: grouped sections (Core, Chat Platform Credentials, Web Build, Advanced Tuning). Every env var read by backend (`Settings` fields in `infra/config.py`), bot (`bot/src/index.ts`), or web (`web/src/lib/api.ts`) must appear with a 1-line comment and a safe default or `__REQUIRED__`.
- [ ] 1.3 Replace `web/README.md` with a Beever-specific overview covering: purpose, `npm install`, `npm run dev`, `npm test`, `npm run lint`, env vars (`VITE_API_URL`, `VITE_BEEVER_API_KEY`, `VITE_BEEVER_ADMIN_TOKEN`).
- [ ] 1.4 Add `src/beever_atlas/README.md` with one line per top-level package.
- [ ] 1.5 Add `bot/README.md` mirroring `web/README.md`'s pattern; add `scripts/README.md` with one line per script.
- [ ] 1.6 Fix `CHANGELOG.md`: backfill `[Unreleased]` with commits since `v0.1.1` (mobile responsiveness, consolidation streaming fix, wiki Key Facts, glossary fix, attachments, brand refresh, security fixes); fix the `compare/HEAD...HEAD` link to `compare/v0.1.1...HEAD`.
- [ ] 1.7 Move `Beever_Atlas_Feature_Spec.docx` out of repo root (to `docs/` or delete); `git rm --cached daily_update.md` if tracked.
- [ ] 1.8 Bump `web/eslint.config.js` `ecmaVersion` to `2022`; delete or fill the commented-out scaffolding in `openspec/config.yaml` lines 3–21.
- [ ] 1.9 Verify: `cp .env.example .env` followed by importing `beever_atlas.infra.config` raises no `MissingEnv`/`KeyError` for documented fields.
- [ ] 1.10 Commit `docs(hygiene): close RES-213 (Q8) — .env.example coverage, READMEs, openspec visibility, CHANGELOG`; move RES-213 → Done with SHA.

## 2. Phase 2 — RES-206 (Q1) CI quality gates

- [ ] 2.1 `.github/workflows/codeql.yml`: delete `continue-on-error: true` and `upload: never` on the `analyze` step.
- [ ] 2.2 Add `pyright` loose config to `pyproject.toml` ([tool.pyright] with `typeCheckingMode: "basic"` and an explicit include list) and install as a dev dep.
- [ ] 2.3 Add `pyright` step to `.github/workflows/ci.yml` backend job.
- [ ] 2.4 Wire `uv run pytest --cov=src/beever_atlas --cov-report=term-missing --cov-fail-under=50` into `ci.yml` backend job; add `[tool.coverage]` config to `pyproject.toml`.
- [ ] 2.5 Add `uv run ruff format --check src/ tests/` step to `ci.yml`.
- [ ] 2.6 Add bot ESLint: `bot/.eslintrc.json` with `@typescript-eslint` rules (`no-explicit-any: error`, `no-unused-vars: error`, `no-floating-promises: error`); `bot/package.json` script `"lint": "eslint src --ext .ts"`; add lint step to `ci.yml` bot job.
- [ ] 2.7 Fix any lint/typecheck/format/coverage findings surfaced by the new gates. If CodeQL finds existing issues, either fix them or file follow-up tickets and add `// codeql[suppress]` with the ticket link.
- [ ] 2.8 Verify: `uv run pyright`, `uv run ruff format --check`, `uv run pytest --cov --cov-fail-under=50`, `cd bot && npm run lint` all exit 0 locally.
- [ ] 2.9 Commit `chore(ci): close RES-206 (Q1) — hard-fail CodeQL + pyright + coverage + ruff format + bot lint`; move RES-206 → Done.

## 3. Phase 3 — RES-194 (H5) Docker digest pins

- [ ] 3.1 Resolve current manifest digests for: `python:3.12-slim`, `node:22-alpine`, `nginx:alpine`, `ghcr.io/astral-sh/uv:<specific-version>` (replace `:latest` with a specific release).
- [ ] 3.2 Resolve digests for compose images: `cr.weaviate.io/semitechnologies/weaviate:1.28.0`, `neo4j:5.26-community`, `mongo:7.0`, `redis:7-alpine`.
- [ ] 3.3 Resolve digests for `docker-compose.nebula.yml`: `vesoft/nebula-graphd:v3.8.0`, `vesoft/nebula-metad:v3.8.0`, `vesoft/nebula-storaged:v3.8.0`, `vesoft/nebula-console:v3.8.0` (as applicable).
- [ ] 3.4 Pin every `FROM` and `image:` by `@sha256:<digest>` while keeping the human-readable `:<tag>` for context (`image: foo:1.2.3@sha256:…`).
- [ ] 3.5 Pin the `COPY --from=ghcr.io/astral-sh/uv:latest` stage — switch to a dated release tag + digest.
- [ ] 3.6 Add a `docker` entry to `.github/dependabot.yml` for `/`, `/web`, `/bot` (so all three Dockerfiles get digest-update PRs); add entries for the compose files if Dependabot supports them in the current version.
- [ ] 3.7 Add a CI lint step (simple grep) that fails on any `FROM` / `image:` without `@sha256:`.
- [ ] 3.8 Verify: `docker compose build` succeeds locally with the pinned digests (or confirm on a host with Docker available).
- [ ] 3.9 Commit `chore(supply-chain): close RES-194 (H5) — digest-pin all Docker base images + Dependabot`; move RES-194 → Done.

## 4. Phase 4 — RES-195 (H6) Chat SDK pinning

- [ ] 4.1 Edit `bot/package.json`: strip `^` from `chat`, `@chat-adapter/{slack,discord,teams,telegram,state-redis}`, and `chat-adapter-mattermost`. Pin to the exact version currently in `bot/package-lock.json`.
- [ ] 4.2 `cd bot && npm install` to regenerate `bot/package-lock.json`; confirm no version drift.
- [ ] 4.3 Edit `bot/Dockerfile` to use `npm ci --audit-signatures` for the prod install stage.
- [ ] 4.4 Add grouped Dependabot rule in `.github/dependabot.yml` for the chat family (`/bot/` ecosystem, group name `chat-sdk-family`, patterns matching the above packages).
- [ ] 4.5 Add a CI lint step that fails on a caret or tilde range in the chat family of `bot/package.json` (simple grep).
- [ ] 4.6 Verify: `cd bot && npm ci --audit-signatures` succeeds; `cd bot && npm test` stays at its current pass count.
- [ ] 4.7 Commit `chore(supply-chain): close RES-195 (H6) — pin chat SDK family + audit-signatures + Dependabot group`; move RES-195 → Done.

## 5. Phase 5 — RES-214 (Q9) Web test harness

- [ ] 5.1 Add idempotent `localStorage` shim to `web/src/test-setup.ts` (install only when `window.localStorage` is absent or its `clear`/`setItem` are not functions) — covers the jsdom 27 prototype-inheritance regression.
- [ ] 5.2 Run `cd web && npm test -- --run` and confirm the 8 `localStorage`-related failures are green.
- [ ] 5.3 Inspect `ChatInputBar` component + the current DOM the test renders; either restore `(N/M)` label format in the component or update `ChatInputBar.tools.test.tsx:46` assertion to match the new label.
- [ ] 5.4 Run `cd web && npm test -- --run` and confirm the `ChatInputBar` failure is green.
- [ ] 5.5 Verify the total: **0 failing specs**, pre-change pass count preserved.
- [ ] 5.6 Commit `fix(web-tests): close RES-214 (Q9) — localStorage shim + ChatInputBar label alignment`; move RES-214 → Done.

## 6. Phase 6 — RES-208 (Q3) Backend test baseline

- [ ] 6.1 In `tests/contracts/test_graph_store_contract.py`, replace each top-level `from beever_atlas.stores.nebula_store import NebulaStore` (lines 74, 237, 256, 277) with a `pytest.importorskip("nebula3")` guarded import (or module-level `pytestmark = pytest.mark.skipif(importlib.util.find_spec("nebula3") is None, reason="nebula extra not installed")`).
- [ ] 6.2 Rewrite `src/beever_atlas/infra/health.py`: wrap each probe in its own typed `try/except`; aggregate into `{status: "healthy"|"degraded"|"unhealthy", failing: [...], checks: {...}}`; handler must never raise; return HTTP 200 always.
- [ ] 6.3 Update `tests/test_health.py` to expect the new contract.
- [ ] 6.4 Skip or service-gate `tests/test_ask_share.py` (11 errors when Mongo/Redis absent) and `tests/test_ask_disabled_tools.py` (4 failures) using `pytest.importorskip` on the service client or a `pytest.mark.integration` marker registered in `pyproject.toml`.
- [ ] 6.5 Replace the enumerated silent `except Exception: pass` sites with `except Exception as exc: logger.debug(...)` at: `api/dev.py:46,53,75,127`; `server/app.py:149`; `services/batch_processor.py:886`; `stores/nebula_store.py:278,489,1613`; `wiki/compiler.py:1899,1912,1922,1924,1934,2318,2405`.
- [ ] 6.6 Add unit tests raising `services/share_store.py` coverage to ≥ 80% using `mongomock`.
- [ ] 6.7 Add unit tests raising `agents/callbacks/quality_gates.py` coverage to ≥ 80%.
- [ ] 6.8 Delete obsolete skipped tests in `tests/test_pdf_chunking.py:74-89` if the functions they reference are truly gone.
- [ ] 6.9 Verify: `uv run pytest` exits 0 with services OFF; coverage floor 50% still passes.
- [ ] 6.10 Commit `fix(backend-tests): close RES-208 (Q3) — importorskip nebula3, health never raises, silent-except bundle, share_store/quality_gates coverage`; move RES-208 → Done.

## 7. Phase 7 — RES-209 (Q4) bridge.ts decomposition

- [ ] 7.1 Create `bot/src/bridge/platformError.ts` containing the canonical hardened `classifyPlatformError` + `PlatformErrorShape` (move from `bot/src/bridge.ts:233–250`). Delete `bot/src/bridge-classifier.ts`.
- [ ] 7.2 Update both test files (`bot/src/bridge-error-classifier.test.ts`, `bot/src/bridge-classifier.test.ts`) to import from `./bridge/platformError.js`. If the two test files cover the same surface, merge into one.
- [ ] 7.3 Create `bot/src/bridge/withPlatformError.ts`: higher-order wrapper `(handler) => async (req, res) => { try { await handler(req, res); } catch (err) { const { status, code } = classifyPlatformError(err); jsonResponse(res, status, { error: String(err), code }); } }`.
- [ ] 7.4 Create `bot/src/http-utils.ts`: move `jsonResponse` out of `bridge.ts`; export it; update `bot/src/index.ts:342,439,447,470,498,506` to import from `http-utils`.
- [ ] 7.5 Create `bot/src/logger.ts`: minimal level-gated logger honoring `LOG_LEVEL`; gate `bridge.ts:1744-1748` debug lines through `logger.debug(...)`.
- [ ] 7.6 Create `bot/src/bridge/app.ts`: owns the Node HTTP server + the route table `ROUTE_TABLE: Array<{ method, pattern, handler }>`. Provide `registerBridgeRoutes` as a thin re-export from here.
- [ ] 7.7 Split route handlers into route modules: `bot/src/bridge/routes/send.ts`, `bot/src/bridge/routes/history.ts`, `bot/src/bridge/routes/files.ts`, `bot/src/bridge/routes/validate.ts`, etc. — one file per route group, each handler wrapped in `withPlatformError`.
- [ ] 7.8 Replace the 201-line `if/else` cascade in `registerBridgeRoutes` with a table-driven dispatcher. Collapse legacy + connection-scoped + platform-prefixed variants via a single resolver.
- [ ] 7.9 Extract magic numbers: `DEFAULT_MESSAGE_LIMIT = 100`, `MAX_MESSAGE_LIMIT = 500` — both in the relevant route module.
- [ ] 7.10 Keep `bot/src/bridge.ts` as a backward-compat re-export shim (`export * from "./bridge/app.js"`) so `bot/src/index.ts` keeps compiling unchanged.
- [ ] 7.11 Verify: `wc -l bot/src/bridge.ts` < 50 (shim); every file under `bot/src/` is < 1,000 lines.
- [ ] 7.12 Verify: `cd bot && npm run build && npm test && npm run lint` all pass; current 116-test baseline preserved (or raised).
- [ ] 7.13 Smoke-test: start backend + bot, create a connection, fetch via `/bridge/channels`, fetch a file via `/bridge/files` — all three endpoints respond with the same shape as before.
- [ ] 7.14 Commit `refactor(bot): close RES-209 (Q4) — decompose bridge.ts, dedupe classifyPlatformError, withPlatformError wrapper`; move RES-209 → Done.

## 8. Cross-cutting verification + PR

- [ ] 8.1 Run the full backend suite (`uv run pytest --cov --cov-fail-under=50`) on the final commit — must be green with zero regressions vs. the 1,140-pass baseline.
- [ ] 8.2 Run `cd bot && npm test && npm run lint && npm run build` — must be green.
- [ ] 8.3 Run `cd web && npm test -- --run && npm run lint && npm run build` — zero failing specs, zero lint errors.
- [ ] 8.4 Run `uv run ruff check .` and `uv run ruff format --check src/ tests/` — clean.
- [ ] 8.5 Run `uv run pyright` — clean (given loose config).
- [ ] 8.6 Run CodeQL locally (or in a draft PR) — no blocking findings, or each one has a tracking ticket.
- [ ] 8.7 Post a close-out comment on Linear RES-177 listing each sub-issue, its commit SHA, and the final test counts.
- [ ] 8.8 Open PR from `feature/res-177-p0-quality-hardening` → `main` with a summary block per closed sub-issue and a test-plan checklist.

## 9. Archive

- [ ] 9.1 After PR merge, run `/opsx:archive res-177-p0-quality-hardening` to fold the seven new capability specs into `openspec/specs/`.
