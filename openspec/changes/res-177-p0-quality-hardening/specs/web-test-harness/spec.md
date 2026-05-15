## ADDED Requirements

### Requirement: `web/src/test-setup.ts` provides a working `localStorage`
The web test setup file SHALL ensure `window.localStorage` has working
`getItem`, `setItem`, `removeItem`, `clear`, `key`, and `length` members
before any test runs. The shim SHALL be installed idempotently so that if
jsdom provides a working `Storage`, the shim does not clobber it.

#### Scenario: `localStorage.clear()` works in every test file
- **WHEN** a test file calls `localStorage.clear()` in `beforeEach`
- **THEN** no `localStorage.clear is not a function` error is raised,
  regardless of jsdom version

#### Scenario: `localStorage.setItem`/`getItem` roundtrip works
- **WHEN** a test calls `localStorage.setItem("k", "v")` and immediately
  reads it back
- **THEN** `localStorage.getItem("k") === "v"`

### Requirement: `web npm test -- --run` is green on `main`
The web test suite SHALL complete with zero failing specs on a fresh
`main` checkout.

#### Scenario: Fresh checkout runs green
- **WHEN** a contributor runs `cd web && npm install && npm test -- --run`
  on `main`
- **THEN** the test command exits 0 with zero failing specs

### Requirement: `ChatInputBar` tool-count label matches its test
The `ChatInputBar` component's tool-count label SHALL match the format
asserted by its tests (either restore `(N/M)` rendering or update the
test to match the current label). Drift between the component and test
SHALL NOT land on `main`.

#### Scenario: Tools label assertion matches rendered DOM
- **WHEN** `web/src/components/channel/__tests__/ChatInputBar.tools.test.tsx`
  runs
- **THEN** every assertion targeting the tool-count label finds a matching
  string in the rendered output
