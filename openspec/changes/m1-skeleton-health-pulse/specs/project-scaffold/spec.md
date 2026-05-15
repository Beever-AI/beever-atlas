## ADDED Requirements

### Requirement: Python package structure
The system SHALL have a `src/beever_atlas/` Python package with the following module directories, each containing an `__init__.py`: `agents/`, `adapters/`, `pipeline/`, `stores/`, `retrieval/`, `wiki/`, `server/`, `infra/`.

#### Scenario: Package is importable
- **WHEN** a Python script runs `import beever_atlas`
- **THEN** the import succeeds without errors

#### Scenario: All submodules exist
- **WHEN** listing directories under `src/beever_atlas/`
- **THEN** directories `agents`, `adapters`, `pipeline`, `stores`, `retrieval`, `wiki`, `server`, `infra` each exist and contain `__init__.py`

### Requirement: Project configuration files
The system SHALL have a `pyproject.toml` at the project root defining the `beever-atlas` package with all required dependencies (FastAPI, google-adk, litellm, weaviate-client, neo4j, pymongo, redis, pydantic, uvicorn).

#### Scenario: Dependencies installable
- **WHEN** running `uv sync` in the project root
- **THEN** all dependencies install successfully

### Requirement: Docker Compose stack
The system SHALL provide a `docker-compose.yml` defining services for: weaviate, neo4j, mongodb, redis, backend (FastAPI), frontend (React), and bot (TypeScript). Each service SHALL have health checks configured.

#### Scenario: Stack starts successfully
- **WHEN** running `docker compose up -d`
- **THEN** all 7 services reach healthy status

#### Scenario: Services are networked
- **WHEN** the backend service starts
- **THEN** it can reach weaviate (port 8080), neo4j (port 7687), mongodb (port 27017), and redis (port 6379) by service name

### Requirement: Environment variable template
The system SHALL provide a `.env.example` file documenting all required environment variables with placeholder values.

#### Scenario: All dependencies covered
- **WHEN** reading `.env.example`
- **THEN** it contains entries for `WEAVIATE_URL`, `WEAVIATE_API_KEY`, `NEO4J_URI`, `NEO4J_AUTH`, `MONGODB_URI`, `REDIS_URL`, `GOOGLE_API_KEY`, `JINA_API_KEY`, `TAVILY_API_KEY`, `ANTHROPIC_API_KEY`

### Requirement: Config system
The system SHALL have a `src/beever_atlas/infra/config.py` module that loads all environment variables into a typed configuration object using Pydantic Settings. Missing required variables SHALL raise a validation error at startup.

#### Scenario: Config loads from environment
- **WHEN** all required env vars are set
- **THEN** `get_settings()` returns a Settings object with all values populated

#### Scenario: Missing required var raises error
- **WHEN** `WEAVIATE_URL` is not set and has no default
- **THEN** `get_settings()` raises a validation error

### Requirement: LiteLLM model routing config
The system SHALL define model routing configuration for two tiers: fast (Gemini Flash Lite with Haiku fallback) and quality (Gemini Flash with Sonnet fallback). Each agent type (fact extraction, entity extraction, query routing, response generation) SHALL be mapped to a tier.

#### Scenario: Fast tier model resolution
- **WHEN** requesting the model for query routing (fast tier)
- **THEN** the config returns `gemini/gemini-2.0-flash-lite` as primary with `anthropic/claude-haiku-4-5` as fallback

#### Scenario: Quality tier model resolution
- **WHEN** requesting the model for response generation (quality tier)
- **THEN** the config returns `gemini/gemini-2.0-flash` as primary with `anthropic/claude-sonnet-4-6` as fallback
