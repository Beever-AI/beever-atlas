## ADDED Requirements

### Requirement: Echo agent as root ADK agent
The system SHALL provide an ADK `LlmAgent` named `query_router_agent` that receives a question from session state and returns a formatted echo response. The agent SHALL use the fast-tier model (`gemini-2.0-flash-lite` via LiteLLM).

#### Scenario: Echo agent processes a question
- **WHEN** the ADK Runner invokes the echo agent with session state `{"question": "what is our tech stack?"}`
- **THEN** the agent returns a response containing the original question echoed back with a preamble indicating it is an echo response

### Requirement: Echo response metadata
The echo agent SHALL include metadata in its response: `route: "echo"`, `confidence: 1.0`, `cost_usd: 0.0`. This validates the metadata pipeline without real routing.

#### Scenario: Echo agent returns metadata
- **WHEN** the echo agent completes processing
- **THEN** the response includes metadata with route "echo", confidence 1.0, and cost_usd 0.0

### Requirement: Agent module structure for future replacement
The echo agent SHALL be defined in `src/beever_atlas/agents/echo.py` and exported via `src/beever_atlas/agents/__init__.py` as `root_agent`. The module structure SHALL match the v2 ADK integration spec so the echo agent can be replaced by the real `query_router_agent` in M3/M4 without changing the Runner wiring.

#### Scenario: Swapping echo agent for real agent
- **WHEN** a developer replaces the echo agent implementation in `agents/__init__.py` with the real `query_router_agent`
- **THEN** the ask endpoint and ADK Runner continue to work without modification because they reference `root_agent` from the agents package

### Requirement: Agent configuration via environment
The agent SHALL read its LLM model configuration from environment variables (`LLM_FAST_MODEL`, `LLM_QUALITY_MODEL`) with defaults matching the v2 spec (`gemini-2.0-flash-lite`, `gemini-2.0-flash`).

#### Scenario: Default model configuration
- **WHEN** no model environment variables are set
- **THEN** the agent uses `gemini-2.0-flash-lite` as the default model

#### Scenario: Custom model override
- **WHEN** `LLM_FAST_MODEL` is set to `claude-haiku-4-5`
- **THEN** the agent uses `claude-haiku-4-5` as its model
