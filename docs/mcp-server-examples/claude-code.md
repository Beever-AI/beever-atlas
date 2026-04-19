# Claude Code MCP Configuration

Claude Code can use Beever Atlas as an MCP server to answer questions about your team's knowledge base without leaving your IDE.

## Setup

### 1. Get an MCP API Key

Contact your Atlas operator to receive a 32-byte hex key, or generate one:
```bash
openssl rand -hex 32
```

Each key identifies one agent instance (e.g., "Claude Code on my laptop"). Keys are stable; store them securely in your shell environment.

### 2. Configure Claude Code

Edit `~/.claude/mcp.json` (or create it if it doesn't exist) and add the Beever Atlas server:

```json
{
  "mcpServers": {
    "beever-atlas": {
      "url": "https://atlas.example.com/mcp/v2",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer ${BEEVER_MCP_KEY}"
      }
    }
  }
}
```

Replace `https://atlas.example.com` with your actual Atlas instance URL.

### 3. Set the Environment Variable

```bash
export BEEVER_MCP_KEY="<your-32-byte-key>"
```

To make this permanent, add it to your shell profile (`.bashrc`, `.zshrc`, etc.):
```bash
echo 'export BEEVER_MCP_KEY="<your-32-byte-key>"' >> ~/.zshrc
source ~/.zshrc
```

### 4. Verify the Connection

In Claude Code, run:
```bash
claude eval "whoami()"
```

You should see a response like:
```json
{
  "principal_id": "mcp:abc123...",
  "connections": ["slack-workspace-123", "discord-server-456"],
  "server_version": "2.0.0"
}
```

If you get a `401 Unauthorized` error, check:
- The `BEEVER_MCP_KEY` env var is set and exported.
- The URL matches your Atlas instance.
- The key is in `BEEVER_MCP_API_KEYS` on the Atlas server.

## Usage

Once configured, Claude Code will discover the Beever Atlas tools automatically. You can:

1. **Ask questions about a channel:**
   ```
   Tell me how authentication works in the DevOps channel
   ```
   Claude Code will call `ask_channel` with streaming citations and follow-ups.

2. **Search for specific facts:**
   ```
   Find discussions about JWT token expiry
   ```
   Claude Code will call `search_channel_facts` with BM25+semantic search.

3. **Browse the wiki:**
   ```
   Show me the FAQ page for the Engineering channel
   ```
   Claude Code will fetch `get_wiki_page` with the rendered content.

4. **Find experts:**
   ```
   Who are the experts on database indexing in the database-team channel?
   ```
   Claude Code will call `find_experts` with ranked results.

5. **Trigger syncs and monitor jobs:**
   ```
   Please sync the latest messages from the feedback channel
   ```
   Claude Code will call `trigger_sync`, poll `get_job_status`, and report when done.

## Troubleshooting

**`401 Unauthorized` on first call:**
- Verify `BEEVER_MCP_KEY` is exported: `echo $BEEVER_MCP_KEY`
- Check the key matches one in your Atlas admin's `BEEVER_MCP_API_KEYS` list.
- Ensure the URL is correct and reachable over HTTPS.

**Claude Code doesn't see Atlas tools:**
- Restart Claude Code after editing `.claude/mcp.json` or setting the env var.
- Check that the `transport` is `"streamable-http"` (not `"sse"`).

**`channel_access_denied` on a tool call:**
- Your principal doesn't own a connection that has selected this channel.
- Ask your Atlas operator to add you as a connection owner or to select the channel.

**Rate limit responses:**
- You hit the per-principal limit (e.g., 30 `ask_channel` calls per minute).
- Wait the number of seconds in `retry_after_seconds` before retrying.

## Limitations

- **No write operations:** You can read and query, but cannot upsert facts, delete knowledge, or edit wiki pages via MCP. These operations are dashboard-only.
- **Per-process rate limits:** If your Atlas has multiple processes, each maintains independent rate limit counters. A distributed rate limiter is planned for v2.
- **Key rotation requires restart:** Changing your key requires restarting the Atlas process. Hot-revocation is planned.

## Further Reading

- See [docs/mcp-server.md](../mcp-server.md) for the full tool catalog, error codes, and operational notes.
- Claude Code docs: https://claude.com/docs/claude-code
