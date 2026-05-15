## ADDED Requirements

### Requirement: One canonical `classifyPlatformError`
`classifyPlatformError` SHALL exist in exactly one source module. Every
import site (tests and production) SHALL resolve to the same symbol.

#### Scenario: Repo has one definition
- **WHEN** a developer greps for
  `export function classifyPlatformError` in the bot source tree
- **THEN** exactly one match is found

#### Scenario: Bug fix does not drift between copies
- **WHEN** a behaviour change is made to `classifyPlatformError`
- **THEN** no further action is required to keep multiple copies in sync,
  because only one copy exists

### Requirement: Every route handler is wrapped by `withPlatformError`
Every bot route handler SHALL return its final JSON response through a
shared `withPlatformError(handler)` wrapper that applies
`classifyPlatformError` on any thrown error and emits a normalised
`{ error, code }` envelope. Inline `try/catch` that duplicates the
classification-plus-envelope pattern SHALL NOT appear in handler bodies.

#### Scenario: New handler inherits error envelope automatically
- **WHEN** a developer adds a new bot route handler and wraps it with
  `withPlatformError`
- **THEN** thrown errors produce the standard `{ status, code, error }`
  envelope without per-handler `try/catch`

### Requirement: Bot source files are under 1,000 LoC
No bot source file in `bot/src/` (excluding generated `dist/` and
`node_modules/`) SHALL exceed 1,000 lines of code. The current
`bot/src/bridge.ts` (2,391 LoC as of the RES-209 audit) SHALL be split
into route modules, a shared app bootstrap, and dedicated utility
modules (`http-utils`, `logger`, `platformError`, `withPlatformError`).

#### Scenario: `bridge.ts` file size drops below the cap
- **WHEN** `wc -l bot/src/bridge.ts` runs after this change
- **THEN** the result is < 1,000

#### Scenario: No other bot source file exceeds the cap
- **WHEN** `find bot/src -name '*.ts' -not -path '*/node_modules/*' | xargs wc -l`
  runs
- **THEN** every non-test file is under 1,000 lines

### Requirement: Route registration uses a table, not regex cascades
`registerBridgeRoutes` SHALL dispatch by a route table
`{ method, pattern, handler }` rather than a flat sequential `if/else`
chain. The table SHALL collapse legacy + connection-scoped +
platform-prefixed variants via a single resolver.

#### Scenario: Adding a route touches one file
- **WHEN** a developer adds a new `/bridge/<endpoint>` route
- **THEN** the change is additive in one route module and one entry in
  the route table — no cascade of `else if` branches is edited

### Requirement: `jsonResponse` is a shared utility
`jsonResponse` (and any sibling HTTP helpers) SHALL live in
`bot/src/http-utils.ts` (or equivalent shared module) and be imported by
both `bot/src/bridge/*` and `bot/src/index.ts`. Inline
`writeHead(…, { "Content-Type": "application/json" })` sites in
`bot/src/index.ts` SHALL be migrated to the shared helper.

#### Scenario: Inline writeHead is gone from index.ts
- **WHEN** a reviewer greps for `writeHead.*application\/json` in
  `bot/src/index.ts`
- **THEN** zero matches remain

### Requirement: Bot uses a level-gated logger
`bot/src/logger.ts` (or equivalent) SHALL provide a minimal level-gated
logging interface (at least `debug`, `info`, `warn`, `error`). The
`LOG_LEVEL` environment variable SHALL control visibility. Bare
`console.*` calls in `bot/src/{bridge/*, index, chat-manager, webhook-buffer}.ts`
SHALL be migrated to the logger (at minimum, debug-only lines SHALL be
gated so prod does not emit them).

#### Scenario: `LOG_LEVEL=info` silences debug lines
- **WHEN** the bot runs with `LOG_LEVEL=info`
- **THEN** `logger.debug(...)` calls produce no output
