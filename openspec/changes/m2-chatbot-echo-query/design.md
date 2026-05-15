## Context

Milestone 1 delivered the project skeleton: FastAPI backend with health endpoint, React frontend shell, Chat SDK bot placeholder (Redis-only), Docker Compose orchestration, and ADK smoke test. The bot service currently just connects to Redis and stays alive — no Chat SDK, no Slack integration.

The v2 architecture specifies a dual-path approach: Python adapters for batch historical ingestion, and a TypeScript Chat SDK bot for real-time chat interaction. M2 establishes both paths and the ADK agent pipeline that connects them to responses.

**Current state:**
- `bot/src/index.ts`: Redis connection only, no Chat SDK
- `src/beever_atlas/`: Package init only, no endpoints beyond health
- `web/`: Vite + React shell, no channel workspace
- No ADK agents beyond the smoke test

**Constraints:**
- Slack is the primary test platform, but the architecture must support Teams, Discord, Linear adapters via Chat SDK
- Python `SlackAdapter` uses `slack-sdk` for batch history (Chat SDK is TypeScript-only, cannot fetch history)
- ADK agents use LiteLLM for model routing (gemini-2.0-flash primary, claude fallback)
- No Weaviate/Neo4j required yet — echo agent validates the pipeline

## Goals / Non-Goals

**Goals:**
- End-to-end interaction loop: Slack @mention → Chat SDK bot → FastAPI SSE endpoint → ADK agent → streamed response → Slack message
- Python adapter layer (`NormalizedMessage` + `SlackAdapter`) ready for M3 batch ingestion
- React channel workspace with streaming Ask tab for browser-based queries
- Validate ADK Runner wiring without external memory stores

**Non-Goals:**
- Actual retrieval from Weaviate or Neo4j (M3/M4)
- Wiki generation or tier consolidation (M5)
- Multi-workspace OAuth for Slack (M8)
- Teams/Discord/Linear adapter implementation (M8, but interfaces designed now)
- Ingestion pipeline stages (M3)

## Decisions

### D1: Chat SDK as the real-time bot framework
**Choice:** Use Vercel Chat SDK (`chat` npm package) with `@chat-adapter/slack` and `@chat-adapter/state-redis`.

**Why:** The v2 spec mandates Chat SDK. It provides a unified adapter interface across Slack/Teams/Discord/Linear with normalized message handling, thread subscriptions, and JSX-based cards. Single codebase for all platforms.

**Alternative considered:** Direct Slack Bolt.js — rejected because it locks us into Slack-only and doesn't provide the multi-platform adapter pattern we need.

### D2: Separate Python adapter for batch ingestion
**Choice:** Python `SlackAdapter` using `slack-sdk` for `conversations.history` / `conversations.replies` batch fetching, independent from the Chat SDK bot.

**Why:** Chat SDK is TypeScript and designed for real-time webhooks, not batch history fetching. The ingestion pipeline (M3) is Python/ADK, so the adapter must be Python. Both paths produce `NormalizedMessage` for downstream processing.

**Alternative considered:** Calling Slack API from TypeScript bot and forwarding to Python — rejected because it adds unnecessary network hops and the ingestion pipeline is entirely Python.

### D3: SSE streaming for the Ask endpoint
**Choice:** `POST /api/channels/:id/ask` returns `text/event-stream` with typed events: `thinking`, `tool_call`, `response_delta`, `citations`, `metadata`, `done`, `error`.

**Why:** The v2 API spec requires SSE streaming. This matches the ADK Runner's streaming output and enables real-time UX in both the React frontend and the Chat SDK bot (which can post-then-edit as tokens arrive).

**Alternative considered:** WebSocket — rejected because SSE is simpler, unidirectional (sufficient for Q&A), and works better with HTTP/2 proxies.

### D4: Echo agent as ADK pipeline validator
**Choice:** Minimal `LlmAgent` that receives a question and returns a formatted echo response with mock metadata (route, confidence, cost). No tools, no memory stores.

**Why:** Validates the full ADK Runner → SSE streaming → response formatting pipeline without requiring Weaviate/Neo4j infrastructure. Can be swapped for the real `query_router_agent` in M3.

**Alternative considered:** Stub the entire ADK layer and return hardcoded JSON — rejected because it doesn't validate the actual ADK Runner streaming behavior.

### D5: BaseAdapter ABC with platform-agnostic interface
**Choice:** Abstract `BaseAdapter` with methods: `fetch_history()`, `fetch_thread()`, `normalize_message()`, `get_channel_info()`, `list_channels()`. Platform adapters implement these.

**Why:** The v2 spec lists Slack, Teams, Discord adapters. A common interface ensures the ingestion pipeline doesn't care which platform messages come from. New adapters just implement the ABC.

### D6: React channel workspace with tab layout
**Choice:** `/channels/:id` route with tab bar (Wiki | Ask | Memories | Graph | Settings). M2 implements Ask tab with SSE streaming consumer; Wiki tab as placeholder.

**Why:** The v2 frontend spec requires this layout. Building the tab infrastructure now lets M3-M5 fill in tabs incrementally.

## Risks / Trade-offs

- **[Chat SDK beta stability]** → Mitigation: Pin exact versions, keep adapter logic thin so we can swap if needed. The core bot logic (forward to backend, format response) is simple.
- **[Slack app permissions]** → Mitigation: Document required OAuth scopes. For M2, single-workspace bot token is sufficient.
- **[SSE connection limits]** → Mitigation: Not a concern for M2 (dev/test scale). Production rate limiting addressed in M7.
- **[ADK echo agent diverges from real agent]** → Mitigation: Echo agent uses the same `LlmAgent` class and Runner streaming interface as the real agent. Only the agent's instructions and tools differ.
- **[Two Slack integration paths]** → Mitigation: Clear separation — Chat SDK owns real-time (TypeScript, webhooks), Python adapter owns batch (history fetch). They share no state except the messages themselves.
