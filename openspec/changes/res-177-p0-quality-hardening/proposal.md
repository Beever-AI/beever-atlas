## Why

The `RES-177` security/quality audit produced eight P0 sub-issues. Six went out
in PR #42 (H1/H2/H3/H4/M1/M6). The remaining **seven P0s** are still open on
`main` — and two have regressed since the audit (`bridge.ts` grew from 1,758 →
2,391 LoC; `tests/contracts/test_graph_store_contract.py` grew from 3 → 4
unguarded `nebula3` imports). Shipping v1.0 OSS with these open leaves us with
floating Docker tags, caret-ranged supply-chain vectors, a CodeQL workflow that
never fails or reports, 9 red frontend tests on `main`, a flaky Python suite,
`.env.example` missing 30+ settings, and a single-file bot bridge that makes
every route edit a shotgun-surgery hazard.

## What Changes

Seven tightly-scoped hardening changes bundled into one delta because they all
feed into the same OSS-launch gate (Linear RES-177 umbrella):

- **RES-213 (Q8)** — Docs, `.env.example` drift, release / repo hygiene. Drop
  `openspec/` from `.gitignore` so new OpenSpec changes are reviewable; rewrite
  `.env.example` to cover every chat-platform token + web + advanced-tuning
  setting consumed in code; fix `CHANGELOG.md` compare link + backfill
  `[Unreleased]`; replace the Vite-scaffold `web/README.md`; move orphaned
  binaries out of repo root; align lint target with tsconfig.
- **RES-206 (Q1)** — CI quality gates. Drop `continue-on-error: true` and
  `upload: never` from CodeQL (stop the security-theatre); add `pyright` loose
  typecheck; add `pytest --cov --cov-fail-under=50`; add `vitest --coverage`;
  add `npm run lint` for bot (with `@typescript-eslint` rules); add
  `ruff format --check`; align `Makefile` with CI once bot lint exists.
- **RES-194 (H5)** — Digest-pin every base image. `Dockerfile`, `web/Dockerfile`,
  `bot/Dockerfile`, `docker-compose.yml`, `docker-compose.nebula.yml`, the
  `COPY --from=ghcr.io/astral-sh/uv:latest` stage. All pinned by
  `@sha256:<digest>`; add Renovate/Dependabot config for digest refresh.
- **RES-195 (H6)** — Pin `chat` SDK family to exact versions (no carets); add
  `npm ci --audit-signatures` to `bot/Dockerfile`; enable Dependabot grouped
  updates so churn in this family is visible.
- **RES-214 (Q9)** — Fix 9 failing web tests. Add a localStorage shim in
  `web/src/test-setup.ts` (covering the jsdom 27 prototype-inheritance
  regression); align `ChatInputBar.tools.test.tsx` expectation with the current
  `(N/M)` label format (or restore the label).
- **RES-208 (Q3)** — Make `uv run pytest` green on a fresh checkout with no
  services running. Gate `nebula3` imports via `pytest.importorskip`; make
  `/api/health` return `degraded` instead of raising when Redis/Mongo probes
  fail; skip service-dependent tests when the service is unreachable; replace
  the bundle of silent `except Exception: pass` sites with typed +
  DEBUG-logged excepts at the locations the ticket enumerates.
- **RES-209 (Q4)** — Decompose `bot/src/bridge.ts` (now 2,391 LoC). Dedupe
  `classifyPlatformError` (canonical implementation lives in a single module;
  both test files import the same source); extract a `withPlatformError`
  wrapper to collapse the 10+ identical `try/catch` envelopes; extract
  `jsonResponse` + logger to shared modules; split route handlers into route
  modules so no bot source file exceeds 1,000 LoC.

## Capabilities

### New Capabilities

- `docs-env-hygiene`: `.env.example` coverage contract, `CHANGELOG` discipline,
  top-level READMEs, repo-root binary policy, `openspec/` tracked-vs-ignored
  contract.
- `ci-quality-gates`: CodeQL hard-fail + SARIF upload, Python typecheck,
  coverage threshold gate, bot lint parity, `ruff format --check`, deploy
  approval + smoke contract.
- `container-supply-chain`: Digest-pinned base images across every Dockerfile
  + compose file + build stage; automated digest-refresh PRs.
- `bot-dependency-pinning`: Exact-version pins for the `chat` SDK family;
  `npm ci --audit-signatures` in `bot/Dockerfile`; Dependabot grouped updates.
- `web-test-harness`: `test-setup.ts` provides a portable `localStorage`
  (and any other web-platform API jsdom strips); every test file inherits it
  without per-file shims.
- `backend-test-baseline`: `uv run pytest` returns green on a fresh checkout
  with **no** live services; optional-extra imports gated by
  `importorskip`; health endpoints never raise; silent-except sites log at
  DEBUG with the exception class.
- `bot-bridge-decomposition`: One canonical `classifyPlatformError`; one
  `withPlatformError` error wrapper; one shared `jsonResponse` + logger; no
  bot source file > 1,000 LoC.

### Modified Capabilities

_None — none of the new capability areas are covered by existing specs under
`openspec/specs/` (only `ask-chat-ui` is present)._

## Impact

- **Code.**
  - `.gitignore`, `.env.example`, `CHANGELOG.md`, `web/README.md`, root READMEs,
    `Beever_Atlas_Feature_Spec.docx` (move), `daily_update.md` (untrack).
  - `.github/workflows/{codeql,ci,audit,deploy,nightly}.yml`, `pyproject.toml`
    ([tool.pyright] + [tool.coverage] if needed), `Makefile`,
    `bot/package.json`, `bot/.eslintrc.*` (new), `bot/tsconfig.json`.
  - `Dockerfile`, `web/Dockerfile`, `bot/Dockerfile`, `docker-compose.yml`,
    `docker-compose.nebula.yml`, `.github/dependabot.yml` or `renovate.json`.
  - `bot/package.json`, `bot/package-lock.json`, `bot/Dockerfile`.
  - `web/src/test-setup.ts`, `web/src/components/channel/__tests__/ChatInputBar.tools.test.tsx`
    (or `ChatInputBar.tsx`).
  - `tests/contracts/test_graph_store_contract.py`, `src/beever_atlas/infra/health.py`,
    `tests/test_health.py`, `tests/test_ask_share.py`, `tests/test_ask_disabled_tools.py`,
    20+ `except Exception: pass` sites enumerated in the Q3 ticket.
  - `bot/src/bridge.ts` → split into `bot/src/bridge/app.ts`,
    `bot/src/bridge/routes/*.ts`, `bot/src/bridge/platformError.ts`,
    `bot/src/bridge/withPlatformError.ts`, `bot/src/http-utils.ts`,
    `bot/src/logger.ts`. Delete `bot/src/bridge-classifier.ts`; update both
    test files to import from the canonical module.
- **APIs.** No user-facing surface breaks. `/api/health` behaviour
  changes from "raise on service error" to "return `{status: degraded, ...}`"
  — **BREAKING** only for clients that currently treat 5xx as "healthcheck
  failed"; ops docs updated.
- **Data migration.** None.
- **Env vars.** None added. `.env.example` now documents settings already
  consumed in code (no new runtime reads).
- **Dependencies.**
  - Bot: pin `chat` family to exact versions (no runtime behaviour change).
  - Backend: add `pyright` and `pytest-cov` as dev deps (already available via
    `uv` for the latter; confirm).
  - CI: introduce Renovate or extend Dependabot config.
