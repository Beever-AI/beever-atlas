## ADDED Requirements

### Requirement: `uv run pytest` is green on a fresh checkout
After `make install`, `uv run pytest` SHALL exit 0 without any live
service running (no Redis, no Mongo, no Weaviate, no Neo4j, no Nebula).
Existing pre-existing-fail tests that were documented as the regression
floor before this change SHALL either be fixed or explicitly marked with
`xfail`/`skip` and an upstream ticket reference.

#### Scenario: Fresh clone passes tests
- **WHEN** a contributor clones the repo, runs `make install`, and runs
  `uv run pytest` with no services running
- **THEN** the command exits 0

### Requirement: Optional-extra imports are guarded
Any Python test module that imports a package declared under an optional
extra (e.g., `nebula3` under `--extra nebula`) SHALL guard the import
with `pytest.importorskip(...)` or a module-level `pytest.mark.skipif`
so that the test file does not fail to collect when the extra is not
installed.

#### Scenario: Nebula tests skip when extra is absent
- **WHEN** `nebula3` is not installed
- **THEN** every test in
  `tests/contracts/test_graph_store_contract.py` that imports
  `beever_atlas.stores.nebula_store` reports as skipped, not errored

### Requirement: `/api/health` never raises
The health endpoint SHALL return HTTP 200 with a JSON body
`{status, failing, …}` regardless of upstream-service reachability. The
`status` field SHALL be `"healthy"`, `"degraded"`, or `"unhealthy"`.
Each probe SHALL be wrapped in its own `try/except`; a failing probe
MUST NOT cause the handler to raise.

#### Scenario: Redis is down
- **WHEN** Redis is unreachable and `GET /api/health` is called
- **THEN** the response is HTTP 200 with
  `status == "degraded"` and `"redis"` listed in `failing`

#### Scenario: All stores are down
- **WHEN** Redis, Mongo, Weaviate, and Neo4j are all unreachable
- **THEN** the response is HTTP 200 with
  `status == "unhealthy"` and every failing probe listed in `failing`

### Requirement: Silent `except Exception: pass` sites log at DEBUG
Enumerated suppression sites in `api/dev.py`, `server/app.py`,
`services/batch_processor.py`, `stores/nebula_store.py`, and
`wiki/compiler.py` (per RES-208 Q3.5) SHALL log the suppressed exception
class and message at DEBUG level before the suppression. Silent bare
excepts without any log SHALL NOT land on `main`.

#### Scenario: A previously-silent failure is now observable
- **WHEN** one of the enumerated suppression sites catches an exception
- **THEN** the suppressed exception class and message are emitted at
  DEBUG level to the configured logger

### Requirement: Coverage for share-store and quality-gates reaches 80%
Unit tests SHALL bring `services/share_store.py` and
`agents/callbacks/quality_gates.py` to ≥ 80% line coverage. These are
two of the nine security-sensitive modules the Q3 ticket flagged at
0–15%; the remaining seven land in a follow-up ticket.

#### Scenario: Coverage floor on share-store
- **WHEN** `uv run pytest --cov=src/beever_atlas/services/share_store
  --cov-fail-under=80` runs
- **THEN** the command exits 0
