## ADDED Requirements

### Requirement: POST /api/channels/:id/ask endpoint
The system SHALL expose `POST /api/channels/:id/ask` that accepts a JSON body with `question` (string, required), `include_citations` (boolean, default true), and `max_results` (integer, default 10). The endpoint SHALL return `Content-Type: text/event-stream`.

#### Scenario: Valid ask request
- **WHEN** a client sends `POST /api/channels/C123/ask` with `{"question": "what is our tech stack?"}`
- **THEN** the endpoint returns HTTP 200 with `Content-Type: text/event-stream` and begins streaming SSE events

#### Scenario: Missing question field
- **WHEN** a client sends `POST /api/channels/C123/ask` with `{}`
- **THEN** the endpoint returns HTTP 422 with a validation error

#### Scenario: Empty question string
- **WHEN** a client sends `POST /api/channels/C123/ask` with `{"question": ""}`
- **THEN** the endpoint returns HTTP 422 with a validation error

### Requirement: SSE event protocol
The endpoint SHALL stream events in the following format: `event: <type>\ndata: <json>\n\n`. The event types SHALL be:
- `thinking`: `{"text": "<reasoning step>"}` — agent's chain-of-thought
- `tool_call`: `{"name": "<tool>", "result_summary": "<brief>"}` — tool invocation
- `response_delta`: `{"delta": "<text chunk>"}` — incremental answer tokens
- `citations`: `{"items": [<citation objects>]}` — citation list
- `metadata`: `{"route": "<route>", "confidence": <float>, "cost_usd": <float>}` — response metadata
- `done`: `{}` — stream complete
- `error`: `{"message": "<error>", "code": "<error_code>"}` — error occurred

#### Scenario: Successful streaming response
- **WHEN** the ADK agent processes a query successfully
- **THEN** the endpoint streams events in order: zero or more `thinking` events, zero or more `tool_call` events, one or more `response_delta` events, one `citations` event, one `metadata` event, and one `done` event

#### Scenario: Agent error during processing
- **WHEN** the ADK agent raises an exception during processing
- **THEN** the endpoint streams an `error` event with the error message and closes the stream

### Requirement: ADK Runner integration
The endpoint SHALL create an ADK Runner instance, invoke the root agent with the user's question as input, and stream the agent's output as SSE events. The Runner SHALL use ADK session state to pass the channel ID and question to the agent.

#### Scenario: Runner invokes the root agent
- **WHEN** a request arrives at the ask endpoint
- **THEN** the system creates an ADK Runner, sets `channel_id` and `question` in session state, and runs the root agent

### Requirement: Request cancellation on client disconnect
The endpoint SHALL detect when the client closes the SSE connection and cancel the in-progress ADK Runner execution to avoid wasted compute.

#### Scenario: Client disconnects mid-stream
- **WHEN** the client closes the SSE connection before the `done` event
- **THEN** the server cancels the ADK Runner execution and cleans up resources
