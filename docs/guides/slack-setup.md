# Slack Integration Setup Guide

This guide covers setting up Slack for Beever Atlas development and testing. The same patterns apply to future platform integrations (Teams, Discord, Linear).

## Prerequisites

- A Slack workspace where you have admin permissions (or can request app installation)
- Node.js 20+ and Docker running locally
- A way to expose localhost to the internet (for Slack webhooks)

## 1. Create a Slack App

1. Go to https://api.slack.com/apps
2. Click **Create New App** → **From scratch**
3. Name: `Beever Atlas (Dev)` — Workspace: your test workspace
4. Note the **Signing Secret** from Basic Information → App Credentials

## 2. Configure Bot Token Scopes

Go to **OAuth & Permissions** → **Scopes** → **Bot Token Scopes** and add:

### Required Scopes (M2)

| Scope | Purpose |
|-------|---------|
| `app_mentions:read` | Receive @mention events |
| `chat:write` | Post responses in channels/threads |
| `channels:history` | Fetch message history (batch ingestion) |
| `channels:read` | List channels and get channel info |
| `files:read` | Read files shared in channels and conversations the app is added to |
| `groups:history` | Fetch private channel history |
| `groups:read` | List private channels |
| `im:history` | Fetch DM history |
| `im:read` | List DMs |
| `users:read` | Resolve user names for NormalizedMessage |
| `reactions:read` | Read reactions on messages |

### Optional Scopes (Future)

| Scope | Purpose |
|-------|---------|
| `reactions:write` | Add reactions to acknowledge messages |
| `mpim:history` | Multi-party DM history |
| `mpim:read` | List multi-party DMs |

## 3. Choose a delivery mode: Socket Mode **or** Events API

Slack can deliver events two ways. **Pick one.**

| | **Socket Mode** (recommended for local / self-hosted) | **Events API** (webhook) |
|---|---|---|
| Transport | Outbound WebSocket from the bot | Slack POSTs to a public URL |
| Needs a public URL / tunnel? | **No** | **Yes** |
| Survives host restarts? | **Yes**, reconnects automatically | Only if the public URL is stable |
| Credential | App-Level Token (`xapp-…`) | Signing Secret |
| Multi-workspace OAuth? | No (single workspace) | Yes |

Socket Mode behaves like the Discord and Mattermost adapters — no tunnel, no re-pointing after a reboot. Prefer it unless you need multi-workspace OAuth (typical only for the hosted/EE multi-tenant build, which runs behind a real public domain).

### Option A — Socket Mode (no public URL needed)

1. **Settings → Socket Mode** → toggle **Enable Socket Mode** ON.
2. **Basic Information → App-Level Tokens** → **Generate Token and Scopes** → add the `connections:write` scope → copy the token (starts with `xapp-`).
3. Still configure **Event Subscriptions → Subscribe to bot events** (below) — the event list applies to both modes — but you do **not** set a Request URL.
4. In the connection wizard (or `SLACK_APP_TOKEN`), provide the `xapp-` token. No signing secret is required.

### Option B — Events API (public Request URL)

Go to **Event Subscriptions** → toggle **Enable Events** ON, then set the **Request URL** to where the bot is reachable from the internet.

Expose the local bot service (port 3001):

```bash
# ngrok — reserve a free STATIC domain so the URL survives restarts:
#   https://dashboard.ngrok.com/domains
ngrok http 3001 --url=https://<your-static>.ngrok-free.app
# …or an ephemeral URL (changes every restart):
ngrok http 3001
# Shortcut: make tunnel NGROK_DOMAIN=<your-static>.ngrok-free.app

# or Cloudflare Tunnel:
cloudflared tunnel --url http://localhost:3001
```

Set **Request URL** to `https://<your-public-url>/api/slack` and provide the **Signing Secret** in the wizard. Slack sends a verification challenge — the Chat SDK handles it automatically.

> **Tip:** Set `PUBLIC_BOT_URL` to this same `https://…` base. The Settings → connection wizard then shows the exact Request URL to paste (and the Teams messaging endpoint), so you never have to assemble it by hand. With a **static** ngrok domain you configure Slack/Teams once and reboots no longer break inbound delivery.

### Subscribe to Bot Events

Add these bot events:

| Event | Trigger |
|-------|---------|
| `app_mention` | When someone @mentions the bot |
| `message.channels` | New message in a public channel the bot is in |
| `message.groups` | New message in a private channel the bot is in |
| `message.im` | New DM to the bot |

## 4. Install the App

1. Go to **OAuth & Permissions** → Click **Install to Workspace**
2. Authorize the requested permissions
3. Copy the **Bot User OAuth Token** (starts with `xoxb-`)

## 5. Environment Variables

Create or update your `.env` file:

```bash
# Slack (required for M2)
SLACK_BOT_TOKEN=xoxb-your-bot-token
SLACK_SIGNING_SECRET=your-signing-secret

# Redis (required for Chat SDK state)
REDIS_URL=redis://localhost:6379

# Backend (bot → Python API)
BACKEND_URL=http://localhost:8000

# LLM (ADK agent)
LLM_FAST_MODEL=gemini-2.5-flash
LLM_QUALITY_MODEL=gemini-2.5-pro
GOOGLE_API_KEY=your-google-api-key
```

## 6. Invite the Bot to Channels

The bot can only see messages in channels it's been invited to:

1. Go to the Slack channel you want to test with
2. Type `/invite @Beever Atlas (Dev)` or click the channel name → Integrations → Add apps

## 7. Testing Checklist

### Smoke Test (M2 Echo Agent)

1. Start all services:
   ```bash
   docker compose up
   ```
2. Verify the bot is online:
   - Check bot service logs for "Chat SDK initialized"
   - The bot should appear as online in Slack
3. Test @mention:
   - In a channel where the bot is invited, type: `@Beever Atlas (Dev) hello`
   - Expected: Bot replies in the thread with an echo response
4. Test follow-up:
   - Reply in the same thread with another message
   - Expected: Bot replies with the echo of the follow-up
5. Test the dashboard Ask tab:
   - Navigate to `http://localhost:5173/channels/<channel-id>`
   - Type a question in the Ask tab
   - Expected: Streaming echo response

### Batch History Test (M2 SlackAdapter)

1. Test via API:
   ```bash
   curl http://localhost:8000/api/channels/<channel-id>/messages?limit=10
   ```
2. Expected: JSON array of normalized messages from the channel

### Common Issues

| Issue | Solution |
|-------|----------|
| Bot doesn't respond to @mentions | Check Event Subscriptions Request URL is correct and reachable |
| `invalid_auth` error | Verify `SLACK_BOT_TOKEN` is correct and the app is installed |
| `channel_not_found` | Invite the bot to the channel first |
| `missing_scope` | Add the required scope and reinstall the app |
| Webhook URL verification fails | Ensure the bot service is running and ngrok/tunnel is active |

## 8. Testing Without Slack (Mock Mode)

For CI/CD and local testing without a real Slack workspace:

- Unit tests mock the Slack API responses
- Integration tests use a mock webhook server that simulates Slack events
- The `NormalizedMessage` and `BaseAdapter` interfaces can be tested with fixture data

## Multi-Platform Notes

### Future: Microsoft Teams
- Register app in Azure AD portal
- Bot Framework registration required
- Adaptive Cards instead of Block Kit
- Use `@chat-adapter/teams` when ready

### Future: Discord
- Create app at discord.com/developers
- Bot token + slash command registration
- Rich embeds instead of Block Kit
- Use `@chat-adapter/discord` when ready

### Future: Linear
- OAuth app in Linear settings
- Webhook subscriptions for issue comments
- Use `@chat-adapter/linear` when ready

The Chat SDK adapter pattern means handler logic (`onNewMention`, `onSubscribedMessage`) stays the same — only the adapter config changes per platform.

## Development Tips

- **Use two channels**: A test channel (just you + bot) for rapid iteration, and your real channel for integration validation
- **ngrok dashboard**: Visit `http://localhost:4040` to inspect webhook payloads
- **Slack API tester**: Use https://api.slack.com/methods to test API calls manually
- **Bot token vs User token**: We use Bot tokens (`xoxb-`) — they have limited scopes but are sufficient for our use case
