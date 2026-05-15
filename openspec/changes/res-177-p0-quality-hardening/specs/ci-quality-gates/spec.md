## ADDED Requirements

### Requirement: CodeQL fails the job on findings and uploads SARIF to Code Scanning
The `.github/workflows/codeql.yml` workflow SHALL NOT set
`continue-on-error: true` on the `analyze` step and SHALL NOT pass
`upload: never`. Vulnerabilities found by CodeQL SHALL fail the CI run
and SHALL surface in the GitHub Security → Code Scanning tab.

#### Scenario: A new CodeQL finding blocks the PR
- **WHEN** a contributor introduces a pattern CodeQL flags
- **THEN** the `Analyze (python)` or `Analyze (javascript-typescript)` job
  fails and the finding appears in the repo's Code Scanning tab

### Requirement: CI enforces a Python coverage floor
The backend CI job SHALL run
`uv run pytest --cov=src/beever_atlas --cov-fail-under=50` (or an equivalent
invocation that fails when coverage drops below the floor). The floor
SHALL be ratcheted upward via follow-up PRs, not lowered.

#### Scenario: A PR that drops coverage below the floor fails CI
- **WHEN** a PR removes test coverage that pushes overall line coverage
  below the configured floor
- **THEN** the backend CI job fails with a coverage-below-threshold error

### Requirement: CI runs a Python typechecker
The backend CI job SHALL run `pyright` (or an equivalent typechecker) with
a documented loose configuration. The configuration SHALL be tightened
over time via follow-up PRs.

#### Scenario: Typechecker failure blocks the PR
- **WHEN** a PR introduces a type error (e.g. calling a function with the
  wrong number of args)
- **THEN** the backend CI job fails at the typecheck step

### Requirement: Bot CI job has lint parity with web
The `bot/` package SHALL declare an `npm run lint` script backed by
`@typescript-eslint` with at minimum the rules `no-explicit-any`,
`no-unused-vars`, and `no-floating-promises`. The `ci.yml` bot job SHALL
invoke `npm run lint` alongside `npm run build` and `npm test`.

#### Scenario: Lint failure blocks the PR
- **WHEN** a PR to the `bot/` package introduces an explicit `any` or a
  floating promise
- **THEN** the bot CI job fails at the lint step

### Requirement: CI enforces `ruff format --check`
The backend CI job SHALL run `uv run ruff format --check src/ tests/`.
Formatting drift SHALL fail the job.

#### Scenario: Unformatted code blocks the PR
- **WHEN** a PR lands Python source with non-canonical formatting
- **THEN** the backend CI job fails at the format step

### Requirement: Coverage floor is discoverable in CI config
The coverage floor value SHALL live in a single source-of-truth (e.g.
`pyproject.toml [tool.coverage.report.fail_under]` or a CI env var) so
future ratchet PRs touch one location.

#### Scenario: Raising the floor is a single-line change
- **WHEN** a maintainer wants to raise the floor from 50 to 55
- **THEN** they can do so by editing one file
