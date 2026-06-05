# Beever Atlas Bot

The Beever Atlas chat bot — a Node HTTP bridge between the Python backend and chat platforms (Slack/Discord/Teams/Telegram/Mattermost) via the Chat SDK.

## Prerequisites

- Node.js 20+
- A running Beever Atlas backend (see root README)

## Commands

| Command | Description |
|---|---|
| `npm install` | Install dependencies |
| `npm run dev` | Start the bot bridge in watch mode (tsx watch) |
| `npm run build` | Compile TypeScript to `dist/` |
| `npm test` | Run Vitest unit tests |

## Entry Points

| File | Role |
|---|---|
| `src/index.ts` | HTTP server entry point — registers platform adapters and starts the Express server |
| `src/bridge.ts` | Route layer — handles inbound webhooks and outbound backend calls |

## Environment Variables

See [`.env.example`](../.env.example) for the canonical list and descriptions. Key variables:

| Variable | Purpose |
|---|---|
| `BRIDGE_API_KEY` | Shared secret for backend ↔ bot bridge auth (REQUIRED) |
| `BRIDGE_ALLOW_UNAUTH` | Dev-only bypass for unauthenticated bridge (must be `"true"`) |
| `BOT_PORT` | Port the bot HTTP server listens on (default: `3001`) |
| `BACKEND_URL` | URL of the Python backend the bot forwards requests to |

### Reply-feature tuning (all optional; safe defaults)

| Variable | Default | Purpose |
|---|---|---|
| `BOT_SESSION_SECRET` | _(dev default)_ | **Set in production.** HMAC key for per-thread conversation-memory session ids; when unset they are predictable to anyone who knows a thread id. |
| `BOT_TRIGGER_REDESIGN` | `on` | Master switch for the gated triggers (mention / 1:1 / quiet-when-humans-join). `off` reverts to legacy behavior but still skips self/other-bots. |
| `BOT_HUMAN_QUIET_THRESHOLD` | `2` | Humans in a thread at/above which the bot withdraws from non-mention follow-ups. |
| `BOT_DM_ENABLED` | `on` | Answer direct messages (private 1:1 Q&A). |
| `BOT_RATELIMIT_PER_MIN` | `12` | Max questions per (platform, channel, user) per minute before a one-time notice, then silent drop. |
| `BOT_ASK_TIMEOUT_MS` | `45000` | Total budget for one `/ask` call (shared across retries). |
| `BOT_PARTICIPANT_CACHE_TTL_MS` | `30000` | TTL cache for a thread's human count (avoids a `getParticipants()` call per non-mention message). `0` disables. |

### Platform Credentials (in `.env.example` section 5a)

| Variable | Platform |
|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | Slack Events API adapter |
| `DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_APPLICATION_ID` | Discord interactions adapter |
| `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD`, `TEAMS_APP_TENANT_ID` | Microsoft Teams / Azure Bot adapter. The bot detects SingleTenant vs MultiTenant from the presence of `TEAMS_APP_TENANT_ID` (see `registerTeamsFromEnvIfPresent` in `src/index.ts`) — SingleTenant is the supported path; MultiTenant requires extra MSAL configuration. Tenant id is also required for any call into `fetchMessages`. |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API adapter |
| `MATTERMOST_BASE_URL`, `MATTERMOST_BOT_TOKEN` | Mattermost outgoing-webhook adapter |

## Further Reading

- [Root README](../README.md) — architecture overview, quick-start, Docker setup
- [CONTRIBUTING.md](../CONTRIBUTING.md) — commit conventions, PR workflow, pre-commit hooks
