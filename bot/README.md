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

### Platform Credentials (in `.env.example` section 5a)

| Variable | Platform |
|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET` | Slack Events API adapter |
| `DISCORD_BOT_TOKEN`, `DISCORD_PUBLIC_KEY`, `DISCORD_APPLICATION_ID` | Discord interactions adapter |
| `TEAMS_APP_ID`, `TEAMS_APP_PASSWORD` | Microsoft Teams / Azure Bot adapter |
| `TELEGRAM_BOT_TOKEN` | Telegram Bot API adapter |
| `MATTERMOST_BASE_URL`, `MATTERMOST_BOT_TOKEN` | Mattermost outgoing-webhook adapter |

## Further Reading

- [Root README](../README.md) — architecture overview, quick-start, Docker setup
- [CONTRIBUTING.md](../CONTRIBUTING.md) — commit conventions, PR workflow, pre-commit hooks
