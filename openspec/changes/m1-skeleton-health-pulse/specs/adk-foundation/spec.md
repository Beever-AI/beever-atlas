## ADDED Requirements

### Requirement: ADK FunctionTool stubs
The system SHALL define `agents/tools.py` containing ADK `FunctionTool` wrappers for all 11 store operations: `search_weaviate_hybrid`, `get_tier0_summary`, `get_tier1_clusters`, `traverse_neo4j`, `temporal_chain`, `comprehensive_traverse`, `get_episodic_weaviate_ids`, `search_tavily`, `upsert_fact`, `upsert_entity`, `create_episodic_link`. Each function SHALL have correct type-annotated signatures and docstrings. In M1, each SHALL raise `NotImplementedError`.

#### Scenario: Tool functions are importable
- **WHEN** running `from beever_atlas.agents.tools import search_weaviate_hybrid`
- **THEN** the import succeeds and the function has a docstring

#### Scenario: Stub tools raise NotImplementedError
- **WHEN** calling `search_weaviate_hybrid(query="test", channel_id="ch1")`
- **THEN** a `NotImplementedError` is raised with a message indicating the store is not yet implemented

#### Scenario: All 11 tools defined
- **WHEN** inspecting `agents/tools.py`
- **THEN** exactly 11 FunctionTool-compatible functions are defined

### Requirement: ADK Runner integration
The system SHALL provide `agents/runner.py` with a function to create an ADK `Runner` with `InMemorySessionService`. The runner SHALL be usable from FastAPI request handlers to execute agent calls.

#### Scenario: Runner creation
- **WHEN** calling `create_runner(agent)` with an ADK agent
- **THEN** a Runner instance is returned with InMemorySessionService configured

#### Scenario: Session creation per request
- **WHEN** a FastAPI request handler needs to run an agent
- **THEN** a new session is created via the session service with a unique ID

### Requirement: LiteLLM integration module
The system SHALL provide `infra/litellm_config.py` that configures LiteLLM model routing for ADK agents. It SHALL define model strings compatible with ADK's `LlmAgent(model=...)` parameter.

#### Scenario: Model string for fast tier
- **WHEN** requesting `get_model("fast")`
- **THEN** returns a LiteLLM-compatible model string for the fast tier

#### Scenario: Model string for quality tier
- **WHEN** requesting `get_model("quality")`
- **THEN** returns a LiteLLM-compatible model string for the quality tier
