# Beever Atlas — AI Agent Install Guide

This file is written for an AI coding agent (e.g. Cline) to install and run the
**Beever Atlas** MCP server with zero human follow-up. Follow the steps exactly.

Beever Atlas is an open-source LLM knowledge base for teams. It turns team chat
(Slack, Discord, Microsoft Teams, Mattermost) into a typed knowledge graph plus an
auto-generated wiki, and exposes all of it through a **28-tool MCP server**.

- Repository: https://github.com/Beever-AI/beever-atlas
- License: Apache-2.0
- Full MCP reference: [`docs/mcp-server.md`](docs/mcp-server.md)
- Contact: tech@beever.ai

---

## Which path should you pick?

| Path | What you get | Backing stores needed | Use when |
| ---- | ------------ | --------------------- | -------- |
| **A. Standalone stdio (introspection)** | The full 28-tool catalog over stdio. `tools/list`, `prompts/list`, `resources/list` work with **zero external dependencies**. Tool *invocations* that read/write knowledge return structured errors (no data). | None | You want to inspect the tool catalog, wire up the MCP client, or run a sandbox without deploying the stack. |
| **B. Full deployment (HTTP `/mcp`)** | The same 28 tools, fully functional against live team knowledge, over authenticated streamable-HTTP at `/mcp`. | MongoDB, Weaviate, Neo4j, Redis (all started by `docker-compose`). | You want real answers from indexed team chat. |

Start with **Path A** to verify everything wires up, then move to **Path B** for real data.

---

## Prerequisites

- **Docker** installed and running (`docker --version`, `docker compose version`).
- Network access to build the image from the repo `Dockerfile`.

> **Note:** A prebuilt image at `ghcr.io/beever-ai/beever-atlas` is **not published yet**.
> Until it is, build locally from the repo `Dockerfile` (Path A/B below) or
> `pip install` / `uv` the package from source. Do **not** reference a pullable
> `ghcr.io` image — it does not exist yet.

---

## Path A — Standalone stdio (introspection-only)

This is the fastest way to get a working MCP server. It needs no databases and no API keys.

### A.1 Build the image

```bash
git clone https://github.com/Beever-AI/beever-atlas.git
cd beever-atlas
docker build -t beever-atlas .
```

### A.2 The stdio entrypoint (read carefully)

> **Important — Docker trap:** the image's default `CMD` is **uvicorn (the HTTP API
> server)**, *not* the MCP stdio entrypoint. For a local stdio MCP you **must override
> the command** with `python -m beever_atlas.api.mcp_server`. Never run a bare
> `docker run beever-atlas` and expect an MCP stdio server — that starts the HTTP API.

Verify the stdio server starts (it speaks MCP over stdin/stdout):

```bash
docker run -i --rm beever-atlas python -m beever_atlas.api.mcp_server
```

The console-script equivalent (if you installed the package directly instead of Docker)
is `beever-atlas-mcp`.

### A.3 MCP client config (stdio)

Add this to your MCP client settings (for Cline: `cline_mcp_settings.json`):

```json
{
  "mcpServers": {
    "beever-atlas": {
      "command": "docker",
      "args": [
        "run", "-i", "--rm",
        "beever-atlas",
        "python", "-m", "beever_atlas.api.mcp_server"
      ],
      "transport": "stdio",
      "disabled": false
    }
  }
}
```

If you installed the Python package directly (no Docker):

```json
{
  "mcpServers": {
    "beever-atlas": {
      "command": "beever-atlas-mcp",
      "args": [],
      "transport": "stdio",
      "disabled": false
    }
  }
}
```

### A.4 Verify

Ask the agent to **list the available tools** from the `beever-atlas` server. You should
see the 28-tool catalog (Discovery, Retrieval, Graph, Session, Orchestration). Then call
`whoami` — in standalone mode it returns a structured "no backing store" error rather than
crashing, which confirms the protocol path works. This is the expected hello-world result
for Path A.

---

## Path B — Full deployment (authenticated HTTP `/mcp`)

This runs the entire stack (Atlas + MongoDB + Weaviate + Neo4j + Redis) and serves the
fully-functional MCP surface at `/mcp` behind Bearer auth.

### B.1 Configure environment

From the repo root, copy the example env file and fill in values:

```bash
cp .env.example .env
chmod 600 .env
```

Generate the required secrets:

```bash
# A 32-byte hex key PER external agent (comma-separated for multiple agents)
openssl rand -hex 32     # -> put in BEEVER_MCP_API_KEYS
openssl rand -hex 32     # -> put in WEAVIATE_API_KEY
openssl rand -hex 32     # -> put in BEEVER_API_KEYS
```

Minimum env vars to set in `.env`:

```bash
# --- MCP server ---
BEEVER_MCP_ENABLED=true                 # mount the /mcp endpoint (default: false)
BEEVER_MCP_API_KEYS=<hex-key-1>         # comma-separated; one key per agent

# --- App / dashboard auth ---
BEEVER_API_KEYS=<hex-key>               # dashboard + REST API key(s)

# --- Backing-store secrets (required by docker-compose) ---
WEAVIATE_API_KEY=<hex-key>
NEO4J_PASSWORD=<password>               # used as neo4j/<password>

# --- LLM + embeddings (knowledge extraction & wiki generation) ---
GOOGLE_API_KEY=<your-gemini-api-key>    # Gemini models for extraction/wiki
EMBEDDING_PROVIDER=<provider>           # e.g. gemini / jina / openai-compatible
EMBEDDING_MODEL=<model-name>
# Optional, provider-dependent:
# EMBEDDING_API_KEY=, EMBEDDING_API_BASE=, JINA_API_KEY=, LLM_FAST_MODEL=, LLM_QUALITY_MODEL=
```

> **Key isolation (server fails fast otherwise):** `BEEVER_MCP_API_KEYS`,
> `BEEVER_API_KEYS`, and `BRIDGE_API_KEY` must be **pairwise disjoint** — no value may
> appear in more than one pool.
>
> Connector credentials (Slack / Discord / Teams / Mattermost tokens) are **not** set in
> `.env`. Connect a workspace through the dashboard: **Settings → Connections**.

### B.2 Launch the stack

```bash
docker compose up -d --build
```

This starts `beever-atlas` plus `weaviate`, `neo4j`, `mongodb`, and `redis`. The API and
`/mcp` mount listen on port `8000` (`http://localhost:8000`). Tail logs with:

```bash
docker compose logs -f beever-atlas
```

### B.3 MCP client config (streamable-HTTP)

Every request must carry `Authorization: Bearer <one of BEEVER_MCP_API_KEYS>`.
Production must serve `/mcp` over TLS (terminate at nginx/Caddy); plaintext HTTP is
unsupported in production.

```json
{
  "mcpServers": {
    "beever-atlas": {
      "url": "https://atlas.example.com/mcp",
      "transport": "streamable-http",
      "headers": {
        "Authorization": "Bearer <your-BEEVER_MCP_API_KEY>"
      }
    }
  }
}
```

For local testing against the compose stack, use `http://localhost:8000/mcp` with the same
Bearer header.

### B.4 Verify

1. Ask the agent to call `whoami` — it returns `{principal_id, connections, server_version}`.
2. Call `list_connections`, then `list_channels(connection_id)` to see real channels.
3. Recommended tool sequence: `whoami` → `list_connections` → `list_channels(connection_id)`
   → any retrieval/graph tool with the `channel_id` you obtained.

---

## Environment variable reference

| Variable | Required | Purpose |
| -------- | -------- | ------- |
| `BEEVER_MCP_ENABLED` | Path B | Mounts the `/mcp` endpoint. Default `false`. |
| `BEEVER_MCP_API_KEYS` | Path B | Comma-separated bearer keys; one per agent. |
| `BEEVER_API_KEYS` | Path B | Dashboard / REST API key(s). |
| `WEAVIATE_API_KEY` | Path B | Auth between backend and Weaviate. |
| `NEO4J_PASSWORD` | Path B | Neo4j password (`neo4j/<password>`). |
| `GOOGLE_API_KEY` | Path B | Gemini API key for extraction + wiki generation. |
| `EMBEDDING_PROVIDER` / `EMBEDDING_MODEL` | Path B | Embedding backend for vector search. |
| `BRIDGE_API_KEY` | Optional | Chat-bridge key; must be disjoint from the two pools above. |
| `JINA_API_KEY`, `TAVILY_API_KEY` | Optional | Alternate embedding / web-search providers. |

Standalone stdio (Path A) needs **none** of these.

---

## Troubleshooting

- **`docker run beever-atlas` started an HTTP server, not stdio MCP.** Expected — the
  default `CMD` is uvicorn. Re-run with the override:
  `docker run -i --rm beever-atlas python -m beever_atlas.api.mcp_server`.
- **`401 Unauthorized` on `/mcp` (Path B).** The `Authorization: Bearer <key>` header is
  missing or the key is not in `BEEVER_MCP_API_KEYS`. User/bridge keys are rejected — MCP
  keys are a separate pool.
- **Server refuses to start, complains about duplicate keys.** A key appears in more than
  one of `BEEVER_MCP_API_KEYS` / `BEEVER_API_KEYS` / `BRIDGE_API_KEY`. Make them disjoint.
- **Tool invocations return "no backing store" errors in stdio mode.** Expected for
  Path A — stdio introspection has no MongoDB/Weaviate/Neo4j/Redis. Use Path B for real data.
- **`list_connections` shows `selected_channel_count: 0` but channels exist.** That field is
  the sync pick-list size, not the platform channel count. Always call `list_channels` for
  the ground truth.

## Usage examples

```text
# Discover topology
whoami
list_connections
list_channels(connection_id="conn_abc123")

# Ask a question against a channel's knowledge
ask_channel(channel_id="C12345", question="How does our auth system work?")

# Read the auto-generated wiki
list_wiki_pages(channel_id="C12345")
read_wiki_page(slug="<page-slug>")
```

See [`docs/mcp-server.md`](docs/mcp-server.md) for the complete tool catalog, auth model,
rate limits, and error catalog.
