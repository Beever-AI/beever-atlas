## 1. Python Adapter Layer (NormalizedMessage + SlackAdapter)

- [x] 1.1 Create `src/beever_atlas/adapters/__init__.py` with package exports
- [x] 1.2 Create `src/beever_atlas/adapters/base.py` with `NormalizedMessage` dataclass, `ChannelInfo` dataclass, and `BaseAdapter` ABC
- [x] 1.3 Add `slack-sdk` dependency to `pyproject.toml`
- [x] 1.4 Create `src/beever_atlas/adapters/slack.py` with `SlackAdapter` implementing `fetch_history`, `fetch_thread`, `get_channel_info`, `list_channels` using `AsyncWebClient`
- [x] 1.5 Add rate-limit handling with exponential backoff in `SlackAdapter`
- [x] 1.6 Create `src/beever_atlas/adapters/mock.py` with `MockAdapter` that reads from fixture JSON files, activated via `ADAPTER_MOCK=true`
- [x] 1.7 Add `get_adapter(platform)` factory function in `__init__.py` that returns `MockAdapter` or real adapter based on env
- [x] 1.8 Create `tests/fixtures/slack_conversations.json` with realistic multi-person conversations (6+ users, 2+ channels, 100+ messages, threads, reactions, decisions, code snippets, spanning 14+ days)
- [x] 1.9 Write tests for `NormalizedMessage`, `SlackAdapter` (mocked API), `MockAdapter` (fixture data), and `get_adapter` factory

## 2. ADK Echo Agent

- [x] 2.1 Create `src/beever_atlas/agents/__init__.py` exporting `root_agent`
- [x] 2.2 Create `src/beever_atlas/agents/echo.py` with echo `LlmAgent` that reads question from session state and returns formatted echo response with metadata
- [x] 2.3 Add model configuration via `LLM_FAST_MODEL` / `LLM_QUALITY_MODEL` env vars with defaults
- [x] 2.4 Write tests for echo agent (verify response format and metadata)

## 3. SSE Streaming Ask Endpoint

- [x] 3.1 Create `src/beever_atlas/api/ask.py` with `POST /api/channels/:id/ask` endpoint
- [x] 3.2 Implement SSE event streaming with typed events (`thinking`, `response_delta`, `citations`, `metadata`, `done`, `error`)
- [x] 3.3 Wire ADK Runner to invoke `root_agent` and stream output as SSE events
- [x] 3.4 Add request validation (question required, non-empty)
- [x] 3.5 Implement client disconnect detection and Runner cancellation
- [x] 3.6 Register the ask router in the FastAPI app
- [x] 3.7 Write tests for ask endpoint (SSE event format, validation errors, streaming)

## 4. Chat SDK Bot (Slack Adapter)

- [x] 4.1 Add `chat`, `@chat-adapter/slack`, `@chat-adapter/state-redis` dependencies to `bot/package.json`
- [x] 4.2 Rewrite `bot/src/index.ts` to initialize Chat SDK with Slack adapter and Redis state
- [x] 4.3 Add webhook route handler (`POST /api/slack` → `bot.webhooks.slack`)
- [x] 4.4 Implement `onNewMention` handler: extract question, call backend `/api/channels/:id/ask`, post response
- [x] 4.5 Implement `onSubscribedMessage` handler for follow-up messages in threads
- [x] 4.6 Create Slack Block Kit response formatter (answer block, citations block, route badge)
- [x] 4.7 Add SSE client to consume backend streaming response and accumulate for posting
- [x] 4.8 Add environment variable validation and graceful startup/shutdown
- [x] 4.9 Write tests for bot handlers and response formatting

## 5. Channels & Messages API Endpoints

- [x] 5.1 Create `src/beever_atlas/api/channels.py` with `GET /api/channels` (list channels via adapter) and `GET /api/channels/:id` (channel info)
- [x] 5.2 Add `GET /api/channels/:id/messages` endpoint returning paginated `NormalizedMessage` list from `SlackAdapter.fetch_history()`
- [x] 5.3 Register channels router in the FastAPI app
- [x] 5.4 Write tests for channels and messages endpoints

## 6. React Channel Workspace

- [x] 6.1 Create `useAsk(channelId)` custom hook for SSE streaming (ask function, response accumulation, thinking, citations, metadata, isStreaming, error)
- [x] 6.2 Create `ChannelWorkspace` component with tab bar (Wiki, Ask, Messages, Graph, Settings)
- [x] 6.3 Create `AskTab` component with question input, streaming response display, collapsible thinking steps, citations, and metadata
- [x] 6.4 Create `WikiTab` placeholder component
- [x] 6.5 Create `MessagesTab` component fetching from `GET /api/channels/:id/messages` with pagination and message display
- [x] 6.6 Create `ChannelList` sidebar component fetching from `GET /api/channels`
- [x] 6.7 Add `/channels/:id` route to the React router
- [x] 6.8 Write tests for `useAsk` hook and component rendering

## 7. Integration & Docker

- [x] 7.1 Update `docker-compose.yml` with Slack env vars (`SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`) and bot service backend URL
- [x] 7.2 Update bot `Dockerfile` to install Chat SDK dependencies and build TypeScript
- [x] 7.3 Run full integration test: bot startup → mock Slack webhook → backend ask endpoint → SSE response
- [x] 7.4 Update Linear issue statuses as tasks are completed
