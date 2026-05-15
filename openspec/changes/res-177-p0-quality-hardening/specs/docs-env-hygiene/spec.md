## ADDED Requirements

### Requirement: `.env.example` documents every runtime-consumed setting
The `.env.example` file at the repo root SHALL contain an entry for every
environment variable that backend, bot, or web code reads at runtime — including
chat-platform credentials (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`,
`DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_APPLICATION_ID`,
`TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`, `TELEGRAM_BOT_TOKEN`), web build-time
vars (`VITE_BEEVER_API_KEY`, `VITE_BEEVER_ADMIN_TOKEN`), and every
`Settings` field declared in `src/beever_atlas/infra/config.py`. Each
entry SHALL carry a one-line comment and a safe default (or the literal
string `__REQUIRED__` for secrets that must be operator-supplied).

#### Scenario: Fresh contributor can bring the stack up from `.env.example`
- **WHEN** a contributor runs `cp .env.example .env` and starts the dev stack
- **THEN** every environment variable read by source code resolves to a
  value documented in `.env.example`, and no `KeyError`/`undefined env var`
  is raised during import of the backend, bot, or web build

#### Scenario: A new `Settings` field requires an `.env.example` entry
- **WHEN** a developer adds a new `Settings` field in
  `src/beever_atlas/infra/config.py`
- **THEN** CI enforces that the new field name appears in `.env.example`
  (via a smoke test or lint)

### Requirement: `openspec/` directory is tracked, not ignored
The `.gitignore` file SHALL NOT ignore the `openspec/` directory. New
OpenSpec changes SHALL appear in `git status` so PR reviewers can see them.

#### Scenario: Creating a new OpenSpec change makes it visible in `git status`
- **WHEN** a developer runs `openspec new change <name>`
- **THEN** `git status` shows the new
  `openspec/changes/<name>/proposal.md` as an untracked file

### Requirement: `CHANGELOG.md` reflects shipped work
The `[Unreleased]` section of `CHANGELOG.md` SHALL list every user-visible
change on `main` that post-dates the most recent git tag. The compare link
at the bottom of the file SHALL point from the most recent tag to `HEAD`,
not `HEAD...HEAD`.

#### Scenario: New PR updates CHANGELOG
- **WHEN** a PR with user-visible changes lands on `main`
- **THEN** `CHANGELOG.md [Unreleased]` lists it (or the repo has a CI step
  that blocks the PR until one is added)

### Requirement: `web/README.md` is Beever-specific, not the Vite scaffold
The `web/README.md` file SHALL describe the Beever Atlas web app, its dev
workflow (`npm run dev`, `npm test`, `npm run lint`), and the environment
variables it consumes (`VITE_API_URL`, `VITE_BEEVER_API_KEY`,
`VITE_BEEVER_ADMIN_TOKEN`). It SHALL NOT be the verbatim Vite scaffold
template.

#### Scenario: Web README answers "what lives here" on first read
- **WHEN** a contributor opens `web/README.md`
- **THEN** it identifies the app as part of Beever Atlas, lists the dev
  scripts, and names the env vars required to run it locally

### Requirement: Root-level binaries and orphan docs are housed correctly
`Beever_Atlas_Feature_Spec.docx` SHALL be moved out of the repo root (into
`docs/` or a Release attachment). `daily_update.md` SHALL be untracked
(it is already present in `.gitignore`). Security review outputs
SHALL live under `docs/security-reviews/` or be excluded from `main`.

#### Scenario: Repo root contains no stray binaries
- **WHEN** a contributor lists the repo root
- **THEN** the only binary-format files at root are those explicitly
  needed for top-level tooling (e.g., `pyproject.toml`, `package.json`,
  not `.docx` spec documents)
