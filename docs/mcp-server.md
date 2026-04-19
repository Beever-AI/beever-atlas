# Beever Atlas MCP Server

## Overview

Beever Atlas exposes a Model Context Protocol (MCP) server at `/mcp` so external AI agents — Claude Code, Cursor, IDE assistants, custom orchestrators — can discover, query, and operate on team knowledge without relying on the dashboard UI. The MCP surface is a curated subset of the internal capability layer that also powers the dashboard's ADK query agent, with unified auth, channel-access enforcement, and long-running job support.

**Who should use this?** AI coding assistants (Claude Code, Cursor) that need to answer questions like *"How does our auth system work?"* or *"Show me the wiki for the DevOps channel"* without a human browsing the dashboard.

**Supported clients:** Claude Code (`.mcp.json` config), Cursor, or any MCP-compatible IDE assistant.

## Enable the MCP Server

### 1. Set Environment Variables

```bash
# Generate a random 32-byte key per external agent
BEEVER_MCP_API_KEYS="agent-key-1,agent-key-2"   # comma-separated
BEEVER_MCP_ENABLED=true                               # enable the /mcp mount (default: false)
```

**Key generation:**
```bash
openssl rand -hex 32
```

Each key identifies one **agent instance** (e.g., "Claude Code in Project X"), not a human user. Keys are stable and long-lived; rotate them by updating `BEEVER_MCP_API_KEYS` and restarting the process.

### 2. Verify Key Isolation

The `BEEVER_MCP_API_KEYS`, `BEEVER_API_KEYS`, and `BRIDGE_API_KEY` pools **must be pairwise disjoint**. The server fails fast at startup if any key value appears in more than one pool.

### 3. Transport Security (Required)

The MCP endpoint MUST be served over TLS in production. This means:
- Terminate TLS at a reverse proxy (nginx, Caddy) or enable native HTTPS on the Atlas process.
- If using a reverse proxy, forward the request to Atlas's plaintext `/mcp` mount internally.
- Configure `Strict-Transport-Security` headers to prevent downgrade attacks.

Plaintext HTTP deployments of the MCP endpoint are explicitly unsupported.

### 4. Key Rotation (Phase 1 Limitation)

In v1, key rotation requires a process restart to remove a compromised key. A forthcoming hot-reload/revocation mechanism (Phase 2+) is tracked separately. For now, operators must:
1. Add the new key to `BEEVER_MCP_API_KEYS`.
2. Restart the process to remove old keys.

## Authentication

Every MCP client request must include an `Authorization: Bearer <key>` header matching a value in `BEEVER_MCP_API_KEYS`. The bearer is validated before any MCP protocol message is dispatched.

**Valid request:**
```json
POST /mcp
Authorization: Bearer <your-agent-key>
Content-Type: application/json

{ "jsonrpc": "2.0", "id": 1, "method": "initialize", ... }
```

**Invalid requests:**
- Missing `Authorization` header → `401 Unauthorized`
- `Authorization: Bearer wrong-key` → `401 Unauthorized`
- Query-string credentials (`?access_token=...`) → `401 Unauthorized` (not supported)
- User or bridge API keys → `401 Unauthorized` (MCP keys are separate)

The server derives a stable **principal id** from each bearer key:
```python
principal_id = f"mcp:{sha256(key)[:16]}"
```

This principal id is used for:
- **Rate limiting** — limits are per-principal, not per-IP, so multiple agents behind one corporate proxy don't throttle each other.
- **Channel-access control** — the principal must own a connection that has selected the target channel.
- **Audit logging** — every tool call is logged with the principal hash (never the raw key).
- **Session continuity** — consecutive `ask_channel` calls from the same principal share one conversation session.

## Tool Catalog

Beever Atlas exposes 16 tools (15 in active use + 1 deprecation shim) grouped by domain. Each tool enforces channel-access policy and returns structured errors.

### Discovery (3 tools)

Tools for exploring your accessible team topology.

#### `whoami`
Returns the authenticated principal id, the list of connections you can access, and the MCP server version. Use this to verify your credentials and understand your scope.

**Returns:** `{principal_id: str, connections: [str, ...], server_version: str}`

#### `list_connections`
List all platform connections (Slack workspaces, Discord servers, Microsoft Teams organizations) accessible to your principal. Returns metadata including platform, display name, sync status, and when it was last synced.

**Returns:** `{connections: [{connection_id, platform, display_name, status, last_synced_at, selected_channel_count, source}, ...]}`

#### `list_channels(connection_id: str)`
List all channels currently selected for sync under a connection you own. Each channel includes sync status, last-sync timestamp, and estimated message count.

**Parameters:**
- `connection_id` — the connection to list channels from (required, e.g., `"slack-workspace-123"`)

**Returns:** `{channels: [{channel_id, name, platform, last_sync_ts, sync_status, message_count_estimate}, ...]}`

**Error:** `{"error": "connection_access_denied", "connection_id": "..."}` if you don't own the connection.

### Retrieval (5 tools)

Tools for searching and reading team knowledge.

#### `ask_channel(channel_id: str, question: str, mode: str = "quick", session_id: str | None = None)`
Ask a natural-language question about a channel's knowledge. The server runs the ADK QA agent in the requested mode, streams thinking and tool-call events as progress notifications, and returns a structured answer with citations and follow-up suggestions.

**Parameters:**
- `channel_id` — target channel (required, e.g., `"C1234567890"`)
- `question` — your question in natural language (required, e.g., `"How does JWT auth work here?"`)
- `mode` — reasoning depth: `"quick"` (~5s, retrieval-only), `"summarize"` (~10s, synthesis), `"deep"` (~30s, tool calls enabled). Default: `"quick"`.
- `session_id` — optional; if omitted, the server maintains one per-principal session for follow-ups.

**Returns:** `{answer: str, citations: [{source, author, timestamp, permalink}, ...], follow_ups: [str, ...], metadata: {...}}`

**Errors:**
- `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.
- `{"error": "answer_timeout"}` if reasoning exceeds 90 seconds.

**Streaming:** While the tool runs, progress notifications are emitted containing thinking chunks, response deltas, and tool-call descriptions. The final result includes structured citations and follow-up questions.

#### `search_channel_facts(channel_id: str, query: str, time_scope: str | None = None, limit: int = 10)`
BM25 + semantic hybrid search over atomic facts in a channel's knowledge base. Returns individual facts with full citation metadata (source message, author, timestamp, permalink).

**Parameters:**
- `channel_id` — target channel (required)
- `query` — search query (required, e.g., `"JWT token expiry"`)
- `time_scope` — optional filter: `"last_7_days"`, `"last_30_days"`, `"all"`. Default: `"all"`.
- `limit` — max results to return (default: 10, max: 100)

**Returns:** `{facts: [{fact, channel_id, author, timestamp, permalink, confidence}, ...]}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.

#### `get_wiki_page(channel_id: str, page_type: str)`
Fetch one pre-generated wiki page. Pages are automatically regenerated nightly and on sync; you can also trigger regeneration via `refresh_wiki`.

**Parameters:**
- `channel_id` — target channel (required)
- `page_type` — one of: `"overview"`, `"faq"`, `"decisions"`, `"people"`, `"glossary"`, `"activity"`, `"topics"` (required)

**Returns:** `{page_type, content, generated_at, citations: [{source, author}, ...]}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.

#### `get_recent_activity(channel_id: str, days: int = 7, topic: str | None = None, limit: int = 20)`
Fetch recent facts and summaries from a channel, optionally filtered by topic and time window.

**Parameters:**
- `channel_id` — target channel (required)
- `days` — look back this many days (default: 7)
- `topic` — optional; filter to this topic (e.g., `"authentication"`)
- `limit` — max results (default: 20, max: 100)

**Returns:** `{facts: [{fact, timestamp, author, topic, permalink}, ...]}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.

#### `search_media_references(channel_id: str, query: str, media_type: str | None = None, limit: int = 5)`
Search for images, PDFs, videos, and links referenced in channel discussions. Useful for finding design specs, architecture diagrams, or documentation links.

**Parameters:**
- `channel_id` — target channel (required)
- `query` — search term (required, e.g., `"architecture diagram"`)
- `media_type` — optional filter: `"image"`, `"pdf"`, `"video"`, `"link"`. Default: all types.
- `limit` — max results (default: 5, max: 20)

**Returns:** `{media: [{url, type, title, source_message, timestamp, author}, ...]}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.

### Graph (3 tools)

Tools for exploring relationships and decision history.

#### `find_experts(channel_id: str, topic: str, limit: int = 5)`
Rank team members by expertise in a topic based on message frequency, citations, and knowledge graph analysis.

**Parameters:**
- `channel_id` — target channel (required)
- `topic` — topic to find experts in (required, e.g., `"database indexing"`)
- `limit` — max results (default: 5)

**Returns:** `{experts: [{name, user_id, confidence, contributions_count}, ...]}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.

#### `search_relationships(channel_id: str, entities: [str], hops: int = 2)`
Graph traversal: given a list of entities (people, projects, decisions), find their connections within N hops. Useful for understanding how decisions connect to projects or who is involved across multiple initiatives.

**Parameters:**
- `channel_id` — target channel (required)
- `entities` — entity names or ids to start from (required, e.g., `["Alice", "ProjectX"]`)
- `hops` — traversal depth (default: 2, max: 4)

**Returns:** `{paths: [{from, to, relationship, distance}, ...]}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.

#### `trace_decision_history(channel_id: str, topic: str)`
Trace the evolution of a decision over time: who proposed it, what alternatives were discussed, why was one chosen, and what happened afterward.

**Parameters:**
- `channel_id` — target channel (required)
- `topic` — decision topic (required, e.g., `"authentication method"`)

**Returns:** `{timeline: [{timestamp, author, event, content, decision, rationale}, ...], current_status}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.

### Orchestration (3 tools)

Long-running operations that return a job_id for polling.

#### `trigger_sync(channel_id: str, sync_type: str | None = None)`
Manually trigger a sync (pull ingestion) for a channel. Returns immediately with a job_id and status URI; poll via `get_job_status` or read the `atlas://job/{id}` resource to track completion.

Idempotent: if a sync for this channel+principal is already queued or running, this returns the existing job_id.

**Parameters:**
- `channel_id` — channel to sync (required)
- `sync_type` — optional; scope of sync, e.g., `"incremental"`, `"full"`. Default: determined by Atlas.

**Returns:** `{job_id, status_uri: "atlas://job/{job_id}", status: "queued" | "running" | "completed" | "failed"}`

**Errors:**
- `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.
- `{"error": "bridge_unavailable"}` if the platform bridge (Slack/Discord/Teams connector) is unreachable.
- `{"error": "rate_limited", "retry_after_seconds": N}` if you exceed 5 syncs per minute.

#### `refresh_wiki(channel_id: str, page_types: [str] | None = None)`
Regenerate wiki pages for a channel. Returns a job_id for polling. If `page_types` is omitted, all pages are regenerated.

**Parameters:**
- `channel_id` — target channel (required)
- `page_types` — optional list to regenerate only these pages, e.g., `["overview", "decisions"]`. Default: all.

**Returns:** `{job_id, status_uri: "atlas://job/{job_id}", status: "queued" | "running" | "completed" | "failed"}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}` if you cannot access the channel.

#### `get_job_status(job_id: str)`
Poll the status of a long-running job (sync or wiki refresh). Returns the current state, progress, result (if complete), and any error.

**Parameters:**
- `job_id` — the job id returned by `trigger_sync` or `refresh_wiki` (required)

**Returns:** `{job_id, kind: "sync" | "wiki_refresh", status: "queued" | "running" | "completed" | "failed", progress_percent, result: {...} | null, error: str | null}`

**Error:** `{"error": "job_not_found"}` if the job does not exist OR is owned by a different principal (no disclosure of ownership).

### Session (1 tool)

#### `start_new_session()`
Archive the current conversation and start a fresh ADK session. Useful when you want to reset context between independent inquiries.

**Returns:** `{session_id, reset_at}`

### Deprecation Shim (1 tool)

#### `search_channel_knowledge` (DEPRECATED)
This tool has been retired. It now returns a structured error so migrating callers know which replacements to use.

**Returns:** `{"error": "tool_renamed", "replacement": ["ask_channel", "search_channel_facts"]}`

**Migration:** Replace calls to `search_channel_knowledge(channel_id, query)` with:
- `ask_channel(channel_id, query, mode="quick")` for natural-language questions with citations, or
- `search_channel_facts(channel_id, query)` for structured fact search.

## Resources

Browsable via MCP's `resources/list` and `resources/read`. These provide alternative access to information that can also be queried via tools.

- **`atlas://connection/{connection_id}`** — connection metadata (platform, display name, status, selected channels)
- **`atlas://connection/{connection_id}/channels`** — list of channels under a connection
- **`atlas://channel/{channel_id}/wiki`** — wiki structure index (list of page types and metadata)
- **`atlas://channel/{channel_id}/wiki/page/{page_id}`** — rendered wiki page content with citations and generation timestamp
- **`atlas://job/{job_id}`** — long-running job status and result (identical to `get_job_status` output)

All resources enforce the same principal-scoped access rules as tools.

## Prompts

Reusable instruction templates for AI agents. Invoked via MCP's `prompts/get`:

- **`summarize_channel(channel_id, since_days=7)`** — Instruction to summarize the last N days of a channel; produces a user-role message naming the channel and timeframe.
- **`investigate_decision(channel_id, topic)`** — Instruction to trace a decision's evolution; produces a message asking the LLM to explore the decision history.
- **`onboard_new_channel(channel_id)`** — Instruction for onboarding to a new channel; produces a message asking for an overview of the channel's purpose and key players.

## Error Catalog

Every tool and resource returns a structured error on failure. The error object always has an `"error"` key with one of these codes:

- **`channel_access_denied`** — you lack access to the named channel. The tool includes a `channel_id` field.
- **`connection_access_denied`** — you do not own the named connection. The tool includes a `connection_id` field.
- **`job_not_found`** — the job does not exist OR is owned by another principal (no disclosure). The tool includes a `job_id` field.
- **`tool_renamed`** — this tool has been deprecated; the error includes a `replacement` field listing the new tool names.
- **`rate_limited`** — you have exceeded the per-principal rate limit for this tool. The error includes `retry_after_seconds` (integer).
- **`cooldown_active`** — a sync is already in progress for this channel; try again in a few moments. The error includes `retry_after_seconds`.
- **`invalid_parameter`** — a parameter failed validation (e.g., malformed channel_id). The error includes a `parameter` field naming the offender.
- **`answer_timeout`** — `ask_channel` exceeded its 90-second hard cap. The partially-accumulated answer is discarded.
- **`bridge_unavailable`** — the platform bridge (Slack/Discord/Teams connector) is unreachable. Returned by sync/refresh operations.

Example error response:
```json
{
  "error": "rate_limited",
  "retry_after_seconds": 45
}
```

## Rate Limits

Per-principal rate limits ensure fair access across multiple agents. Limits are keyed on the principal id (derived from your bearer token), not client IP, so multiple agents behind one corporate proxy do not throttle each other.

| Tool(s) | Limit | Behavior on Exceed |
|---------|-------|-------------------|
| `ask_channel` | 30 per minute | Returns `{"error": "rate_limited", "retry_after_seconds": N}` |
| `trigger_sync`, `refresh_wiki` | 5 per minute | Returns `{"error": "rate_limited", "retry_after_seconds": N}` |
| All other tools | 60 per minute | Returns `{"error": "rate_limited", "retry_after_seconds": N}` |

Limits reset every 60 seconds. If you hit a limit, the response tells you how many seconds to wait before retrying.

**Note:** The rate limiter is per-process and not distributed across multiple Atlas instances. In v1, if Atlas is deployed behind a load balancer, each process maintains independent per-principal counters. This is acceptable for team deployments; a forthcoming distributed rate limit (Phase 2+) is tracked separately.

## Long-Running Jobs

Sync and wiki-refresh operations may take seconds to minutes. Instead of blocking, these tools return immediately with a job id and status URI.

### Job Pattern

1. Call `trigger_sync` or `refresh_wiki` → returns `{job_id, status_uri: "atlas://job/{id}", status: "queued"}`
2. Poll via:
   - **Tool:** `get_job_status(job_id)` → returns `{status, progress_percent, result, error}`
   - **Resource:** `resources/read` on `atlas://job/{id}` → returns the same payload
3. On completion, the `status` field becomes `"completed"` (on success) or `"failed"` (on error).

### Progress Reporting

For jobs that complete inside the initial request window (~30 seconds), the MCP server emits **progress notifications** while the job runs. The client's MCP library surfaces these as streamed updates. Agents like Claude Code display these as real-time feedback during long operations.

For longer jobs, only the final `get_job_status` or resource read will show completion.

## Known Limitations (v1)

- **`ask_channel` ADK runner stub in Phase 3:** In Phase 3 of the rollout, the `ask_channel` tool invokes the full ADK QA agent. However, in Phase 1 (current), external agents cannot mutate knowledge — no tools to upsert facts, delete memories, or edit wiki pages. Write operations remain dashboard-only.
- **In-memory rate-limit state:** Each Atlas process maintains rate-limit state in memory. With multiple processes behind a load balancer, rate limits are per-process. A distributed rate-limit store (Redis) is planned for Phase 2+.
- **Key rotation requires restart:** Changing `BEEVER_MCP_API_KEYS` requires restarting the process. A hot-reload/revocation mechanism (Phase 2+) is tracked separately.
- **MCP sessions isolated from dashboard:** Sessions created via MCP are not visible in the dashboard `/api/ask/sessions` endpoint, and vice versa. The two session models are independent.
- **Dashboard operator view deferred:** A minimal dashboard view for operators to monitor MCP tool call volume by principal is deferred to v1.1.

## Example Client Configurations

### Claude Code (`.mcp.json`)

Claude Code reads MCP server configs from `~/.claude/mcp.json` (or per-project `.claude/mcp.json`).

```json
{
  "mcpServers": {
    "beever-atlas": {
      "url": "https://atlas.example.com/mcp",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer ${BEEVER_MCP_KEY}"
      }
    }
  }
}
```

Then set the environment variable:
```bash
export BEEVER_MCP_KEY="<your-32-byte-hex-key>"
```

Verify the connection:
```bash
claude eval "whoami()"
```

This should return your principal id and accessible connections.

### Cursor

Cursor uses the `mcp-remote` proxy for HTTP transports (since Cursor's native config is stdio-focused).

```json
{
  "mcpServers": {
    "beever-atlas": {
      "command": "mcp-remote",
      "args": [
        "--url", "https://atlas.example.com/mcp",
        "--header", "Authorization: Bearer $BEEVER_MCP_KEY"
      ]
    }
  }
}
```

Set the environment variable:
```bash
export BEEVER_MCP_KEY="<your-32-byte-hex-key>"
```

Then use Cursor normally; it will discover Atlas tools in the context menu or symbol search.

## Getting Started

1. **Generate a key:**
   ```bash
   openssl rand -hex 32
   ```

2. **Add to environment:**
   ```bash
   export BEEVER_MCP_API_KEYS="<your-key>"
   export BEEVER_MCP_ENABLED=true
   ```

3. **Configure your client:**
   - For Claude Code: paste the config above into `~/.claude/mcp.json` and set `BEEVER_MCP_KEY`.
   - For Cursor: same config, different integration point.

4. **Test the connection:**
   ```bash
   # Your MCP client's CLI, e.g. for Claude Code:
   claude eval "whoami()"
   ```

5. **Explore:**
   ```bash
   # List your connections and channels
   claude eval "list_connections()"
   claude eval "list_channels(connection_id='...')"
   
   # Ask a question
   claude eval "ask_channel(channel_id='...', question='How does auth work?')"
   ```

## Troubleshooting

**`401 Unauthorized` on initial request:**
- Verify the bearer token in your config matches a key in `BEEVER_MCP_API_KEYS` (comma-separated).
- Ensure the header is `Authorization: Bearer <key>`, not `Authorization: Bearer: <key>` (no colon after Bearer).
- Check that TLS is enabled (the endpoint must be `https://`, not `http://`).

**`rate_limited` responses:**
- You have hit the per-principal limit for this tool. Wait `retry_after_seconds` before retrying.
- Limits reset every 60 seconds per principal. Check if you have other agents using the same key.

**`channel_access_denied`:**
- Your principal does not own a connection that has selected this channel. Ask your Atlas operator to add you as a connection owner or to select the channel in your connections.

**Progress notifications not showing:**
- The MCP client's library must support progress notifications. Claude Code 1.x and Cursor 0.35+ support them. Older clients may not display streaming updates.

**Empty results from retrieval tools:**
- The channel may not have been synced yet (check `last_sync_ts` via `list_channels`). Trigger a sync with `trigger_sync(channel_id)` and wait for it to complete.
- Or the channel may have no facts matching your query. Try a broader search term.

## Security Considerations

- **Bearer tokens are sensitive.** Store them as environment variables, not in code or version control.
- **Keys are principal identifiers.** Every agent deployment should have its own key. If a key is compromised, revoke it by removing it from `BEEVER_MCP_API_KEYS` and restarting (v1 limitation; hot-revocation planned for v2).
- **Raw keys are never logged.** Only the stable principal id (`mcp:xxxxxxx…`) appears in logs and audit trails.
- **TLS is required.** Plaintext HTTP is explicitly unsupported. Ensure your reverse proxy or native HTTPS is configured correctly.
- **Channel access is enforced.** The principal must own a connection that has selected the target channel. Cross-channel browsing by a malicious principal is prevented.

## Further Reading

- `openspec/changes/atlas-mcp-server/` — OpenSpec design docs, including auth specs and error catalogs.
- MCP specification: https://modelcontextprotocol.io/
- Beever Atlas README: see the main [README.md](../README.md) for architecture and capabilities overview.
