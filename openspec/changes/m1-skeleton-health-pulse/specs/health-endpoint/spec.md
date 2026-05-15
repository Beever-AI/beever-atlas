## ADDED Requirements

### Requirement: FastAPI application entry point
The system SHALL provide a FastAPI application in `src/beever_atlas/server/app.py` with CORS middleware configured to allow the React dev server (localhost:5173) and production origins.

#### Scenario: App starts successfully
- **WHEN** running `uvicorn beever_atlas.server.app:app`
- **THEN** the FastAPI server starts and accepts HTTP requests

#### Scenario: CORS allows React dev server
- **WHEN** a request arrives from `http://localhost:5173` with an `Origin` header
- **THEN** the response includes appropriate CORS headers allowing the request

### Requirement: Health endpoint
The system SHALL expose `GET /api/health` that checks connectivity to Weaviate, Neo4j, MongoDB, and Redis. It SHALL return a `HealthResponse` JSON with overall status and per-component details including status and latency in milliseconds.

#### Scenario: All services healthy
- **WHEN** all 4 data stores are reachable
- **THEN** response status is 200, overall status is "healthy", and each component shows status "up" with latency < timeout

#### Scenario: One service down
- **WHEN** Neo4j is unreachable but others are up
- **THEN** response status is 200, overall status is "degraded", Neo4j component shows status "down" with error message, others show "up"

#### Scenario: All services down
- **WHEN** no data stores are reachable
- **THEN** response status is 200, overall status is "unhealthy", all components show status "down"

### Requirement: DependencyHealth registry
The system SHALL provide an `infra/health.py` module with a `DependencyHealth` class that maintains a registry of health check functions for each dependency. Each check SHALL have a configurable timeout (default 5 seconds).

#### Scenario: Register a health check
- **WHEN** calling `registry.register("weaviate", check_fn, timeout=5.0)`
- **THEN** the check function is stored and callable via `registry.check_all()`

#### Scenario: Health check timeout
- **WHEN** a health check function takes longer than its timeout
- **THEN** the component reports status "down" with error "timeout"

### Requirement: HealthResponse schema
The system SHALL define Pydantic models for health responses: `HealthResponse` (status: str, components: dict, timestamp: str) and `ComponentHealth` (status: str, latency_ms: float, error: Optional[str]).

#### Scenario: Response serialization
- **WHEN** the health endpoint returns
- **THEN** the JSON matches the schema with status being one of "healthy", "degraded", "unhealthy"
