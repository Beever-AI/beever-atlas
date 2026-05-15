## Why

Milestone 1 established the skeleton and health pulse. Now we need the end-to-end interaction loop: a user @mentions Beever in Slack, the system processes the query through ADK agents, and returns a response. This is the foundation for all future retrieval — without a working chat-to-response path, no downstream milestone (ingestion, retrieval, wiki) can be validated by real users. We also need the Python-side `NormalizedMessage` adapter layer to support batch ingestion of Slack history, which Milestone 3 depends on.

## What Changes

- **Chat SDK bot service** (`bot/`): Replace the Redis-only placeholder with a full Chat SDK (`chat` npm package) integration using `@chat-adapter/slack` and `@chat-adapter/state-redis`. Wire `onNewMention` and `onSubscribedMessage` handlers that forward queries to the Python backend's `/api/channels/:id/ask` endpoint and render responses as Slack Block Kit messages.
- **SSE streaming Q&A endpoint**: `POST /api/channels/:id/ask` accepts a question and streams ADK Runner output as Server-Sent Events (`thinking`, `tool_call`, `response_delta`, `citations`, `metadata`, `done`, `error`).
- **ADK echo agent**: A minimal ADK agent (query_router_agent shell) that receives a question via the SSE endpoint and returns an echo response. This validates the full ADK Runner wiring without requiring Weaviate/Neo4j.
- **NormalizedMessage & SlackAdapter (Python)**: The platform adapter layer (`src/beever_atlas/adapters/`) with `NormalizedMessage` dataclass, `BaseAdapter` ABC, and `SlackAdapter` implementation for batch history fetching via `slack-sdk`.
- **React channel workspace**: Tab layout (`/channels/:id`) with Wiki tab (placeholder), Ask tab (streaming SSE consumer), and basic channel list sidebar.

## Capabilities

### New Capabilities
- `chat-bot`: Chat SDK bot setup with Slack adapter, webhook routing, @mention/subscription handlers, and multi-adapter architecture for future platforms (Teams, Discord, Linear)
- `ask-endpoint`: SSE streaming `/api/channels/:id/ask` endpoint with ADK Runner integration and event protocol
- `adk-echo-agent`: Minimal ADK agent that echoes queries back, validating the full agent pipeline wiring
- `normalized-message`: `NormalizedMessage` model, `BaseAdapter` ABC, and `SlackAdapter` for batch message history fetching
- `channel-workspace`: React channel workspace with tab layout, Ask tab with streaming consumer, and channel list

### Modified Capabilities
<!-- No existing specs to modify -->

## Impact

- **bot/**: Major rewrite — add `chat`, `@chat-adapter/slack`, `@chat-adapter/state-redis` dependencies; new webhook route, event handlers, Slack Block Kit formatter
- **src/beever_atlas/**: New `adapters/` package, new `api/ask.py` endpoint, new `agents/echo.py` ADK agent
- **web/**: New channel workspace page, Ask tab component, SSE streaming hook
- **docker-compose.yml**: Bot service needs `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` env vars
- **Dependencies**: `chat`, `@chat-adapter/slack`, `@chat-adapter/state-redis` (npm); `slack-sdk` (Python)
- **Linear issues**: RES-96 (Chat SDK bot), RES-101 (SSE endpoint), RES-102 (React workspace), RES-75 (NormalizedMessage + SlackAdapter)
