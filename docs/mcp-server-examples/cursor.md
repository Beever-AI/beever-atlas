# Cursor MCP Configuration

Cursor is an IDE that integrates with Beever Atlas via the Model Context Protocol to let you ask questions about your team's knowledge base while coding.

## Setup

### 1. Get an MCP API Key

Contact your Atlas operator to receive a 32-byte hex key, or generate one:
```bash
openssl rand -hex 32
```

Each key identifies one agent instance (e.g., "Cursor on my laptop"). Keys are stable; store them securely.

### 2. Configure Cursor

Cursor's MCP config uses the `mcp-remote` proxy wrapper for HTTP transports (Cursor's native config is stdio-focused).

Edit **Cursor settings** (typically in `.cursor/settings.json` or via the Cursor UI) and add:

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

Replace `https://atlas.example.com` with your actual Atlas instance URL.

Note: Cursor uses environment variable interpolation with the `$VARIABLE` syntax (not `${VARIABLE}`).

### 3. Set the Environment Variable

```bash
export BEEVER_MCP_KEY="<your-32-byte-key>"
```

To make this permanent, add it to your shell profile:
```bash
echo 'export BEEVER_MCP_KEY="<your-32-byte-key>"' >> ~/.zshrc
source ~/.zshrc
```

### 4. Verify the Connection

Restart Cursor and open a file. You should see Beever Atlas tools available in:
- The **MCP Tools** panel (if available in your Cursor version).
- Symbol search (Cmd+K / Ctrl+K).
- Inline code suggestions and context.

You can test by opening the **MCP Tools** panel and calling `whoami()`. You should see:
```json
{
  "principal_id": "mcp:abc123...",
  "connections": ["slack-workspace-123", "discord-server-456"],
  "server_version": "2.0.0"
}
```

If you get a `connection refused` or `401` error:
- Check that `BEEVER_MCP_KEY` is set: `echo $BEEVER_MCP_KEY`
- Verify the URL is correct and reachable.
- Ensure the key is in your Atlas operator's `BEEVER_MCP_API_KEYS` config.
- Restart Cursor after setting the env var.

## Usage

Once configured, Cursor will offer Beever Atlas tools in its AI context. You can:

1. **Ask about team knowledge in your IDE:**
   > "What does the authentication system in the DevOps channel do?"
   
   Cursor will call `ask_channel` and integrate citations into its response.

2. **Search for code patterns or decisions:**
   > "How do we handle JWT token validation? Look it up in the Auth channel."
   
   Cursor will call `search_channel_facts` to find relevant discussions.

3. **Get wiki pages for reference:**
   > "Show me the FAQ for the Backend team."
   
   Cursor will fetch the pre-generated wiki page.

4. **Find subject matter experts:**
   > "Who should I talk to about database optimization?"
   
   Cursor will call `find_experts` and suggest team members.

5. **Sync latest knowledge:**
   > "Refresh the knowledge for the API channel to get the latest discussions."
   
   Cursor will trigger a sync and monitor progress.

## Troubleshooting

**`connection refused` or timeout:**
- Verify the Atlas URL is correct and accessible: `curl -I https://atlas.example.com/mcp`
- Check that TLS is enabled (URL must be `https://`).
- Confirm your network can reach the Atlas server (no firewall blocking).

**`401 Unauthorized`:**
- Check that `BEEVER_MCP_KEY` is set and exported in your shell: `echo $BEEVER_MCP_KEY`
- Verify the key is in your Atlas admin's `BEEVER_MCP_API_KEYS` list.
- Restart Cursor after updating the env var so it picks up the new value.

**Cursor doesn't see Atlas tools:**
- Restart Cursor completely (quit and reopen).
- Check the Cursor MCP settings in your config file — ensure `command` is `mcp-remote` and args are correctly formatted.
- Make sure `mcp-remote` is installed and in your `PATH`. Install it via `npm install -g @modelcontextprotocol/server-mcp-remote` if needed.

**`channel_access_denied` errors:**
- Your principal doesn't own a connection that has selected this channel.
- Ask your Atlas operator to grant you access to the connection or to select the channel.

**Rate limit responses:**
- You hit the per-principal rate limit (e.g., 30 `ask_channel` calls per minute).
- Wait the number of seconds in `retry_after_seconds` before retrying.

## Limitations

- **No write operations:** You can query and read, but not upsert facts, delete knowledge, or edit wiki pages via MCP. These are dashboard-only.
- **Per-process rate limits:** If Atlas runs on multiple processes, each has independent rate-limit counters. A distributed rate limiter is planned.
- **Key rotation requires restart:** Changing your key requires restarting Atlas. Hot-revocation is planned.

## Further Reading

- See [docs/mcp-server.md](../mcp-server.md) for the full tool catalog, error codes, rate limits, and operational guidance.
- Cursor docs: https://docs.cursor.com
- MCP specification: https://modelcontextprotocol.io/
