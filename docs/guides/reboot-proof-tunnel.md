# Keep Slack & Teams working across reboots

Some chat platforms deliver messages **inbound** to a public webhook, so they
need a publicly reachable URL:

| Platform | Transport | Needs a public URL/tunnel? |
|---|---|---|
| Discord | Outbound WebSocket (Gateway) | No |
| Mattermost | Outbound WebSocket | No |
| **Slack — Socket Mode** | Outbound WebSocket | **No** |
| Slack — Events API | Inbound webhook | Yes |
| **Microsoft Teams** | Inbound webhook (Bot Framework) | **Yes — always** |

In local dev that public URL is usually an ngrok tunnel. When the machine
reboots, the tunnel dies and any **ephemeral** URL changes — so Slack (Events
API) and Teams stop receiving messages until the tunnel is restarted and their
endpoints are re-pointed. (Discord, Mattermost, and **Slack Socket Mode** are
unaffected — prefer Socket Mode for Slack to drop it from this list entirely;
see [slack-setup.md](./slack-setup.md).)

Teams has no Socket Mode equivalent, so it always needs this. Two ways to make
it survive reboots:

## Option A — Static domain (recommended)

A reserved domain never changes, so you configure Slack/Teams **once**.

1. Reserve the free static domain at <https://dashboard.ngrok.com/domains>.
2. Put it (and your Teams app id) in `.env`:
   ```ini
   NGROK_DOMAIN=your-name.ngrok-free.app
   PUBLIC_BOT_URL=https://your-name.ngrok-free.app
   TEAMS_APP_ID=<id from `teams app list`>
   ```
3. Point Teams' messaging endpoint (and Slack's Request URL, if you use Events
   API) at `https://your-name.ngrok-free.app/api/teams` once.
4. Start the tunnel: `make tunnel NGROK_DOMAIN=your-name.ngrok-free.app`
   (or use the launchd agent below to auto-start it).

## Option B — `tunnel-up` auto-sync (works with an ephemeral URL too)

`scripts.tunnel_up` starts the tunnel, writes `PUBLIC_BOT_URL` into `.env`,
restarts the backend, and re-points the Teams messaging endpoint — then holds
the tunnel open. With a static domain it's effectively one-time; with an
ephemeral URL it re-syncs every start.

```bash
make tunnel-up                 # start + sync + hold
make tunnel-up DRY_RUN=1       # preview without changing anything
# flags: --no-restart, --no-teams, --domain, --teams-app-id
```

It reads `NGROK_DOMAIN` / `TEAMS_APP_ID` / `BOT_PORT` from `.env`. Teams
re-pointing needs the [Teams CLI](https://aka.ms/teams-cli) logged in (`teams
login`) and `TEAMS_APP_ID` set; otherwise that step is skipped with a notice.

## Auto-start at login (macOS launchd)

So the tunnel comes up on every reboot without you running anything:

```bash
make tunnel-install      # installs + loads ~/Library/LaunchAgents/ai.beever.tunnel.plist
# ...
make tunnel-uninstall    # stop + remove it
```

The agent runs `tunnel-up` with `RunAtLoad` + `KeepAlive`, so it starts at login
and, if ngrok dies, relaunches and re-syncs. Logs: `/tmp/beever-tunnel.out.log`
and `/tmp/beever-tunnel.err.log`. The template lives at
`deploy/launchd/ai.beever.tunnel.plist.template`.

## Production

No tunnel: set `PUBLIC_BOT_URL` to your real domain (e.g.
`https://beever-atlas.example.com`) and point Slack/Teams at it. The webhook
paths are `/api/slack` and `/api/teams`.
