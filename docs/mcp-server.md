# Beever Atlas MCP Server

## Overview

Beever Atlas exposes a Model Context Protocol (MCP) server at `/mcp` so external AI agents — Claude Code, Cursor, IDE assistants, custom orchestrators — can discover, query, and operate on team knowledge without relying on the dashboard UI. The MCP surface is a curated subset of the internal capability layer that also powers the dashboard's ADK query agent, with unified auth, channel-access enforcement, and long-running job support.

**Who should use this?** AI coding assistants (Claude Code, Cursor) that need to answer questions like *"How does our auth system work?"* or *"Show me the wiki for the DevOps channel"* without a human browsing the dashboard.

**Supported clients:** Claude Code (`.mcp.json` config), Cursor, or any MCP-compatible IDE assistant.

## Standalone Introspection Mode (stdio)

Running `python -m beever_atlas.api.mcp_server` (or the installed `beever-atlas-mcp` console script) serves the **same curated MCP surface over stdio** — no HTTP server, no `/mcp` mount, and no backing stores (MongoDB, Weaviate, Neo4j, Redis). The server starts instantly and answers protocol handshakes from a clean environment.

**Who it's for:** MCP registries (Glama.ai), local launchers (`mcp-proxy`, Claude Desktop), and anyone who wants to inspect the tool catalog without deploying the full stack.

**Key contract:** catalog introspection — `tools/list`, `prompts/list`, `resources/list`, and their `*/get` counterparts — needs **zero external dependencies** and works against a bare container. Tool *invocations* that read or write knowledge still require the backing stores; in this mode they return the same structured errors documented in the [Error Catalog](#error-catalog) rather than crashing.

**Auth:** stdio is a local single-principal transport, so the HTTP `Authorization: Bearer` middleware does not apply (and there is no store data to protect). Production deployments should keep using the authenticated `/mcp` mount described below.

```bash
# From a built image (e.g. a registry sandbox)
docker run -i --rm beever-atlas python -m beever_atlas.api.mcp_server

# From a local checkout
uv run beever-atlas-mcp
```

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

Beever Atlas exposes **28 tools** (27 in active use + 1 deprecation shim) grouped by domain: Discovery (3), Retrieval (17), Graph (3), Session (1), and Orchestration (3). Each tool enforces principal-scoped channel/connection access policy and returns structured error payloads (see the [Error Catalog](#error-catalog)).

**Recommended entry sequence:** `whoami` → `list_connections` → `list_channels(connection_id)` → then any retrieval/graph tool with the `channel_id` you obtained. `list_channels` is the ground truth for which channels exist — do not infer availability from `list_connections.selected_channel_count`.

> **Wiki tool families.** There are two wiki-page surfaces. `get_wiki_page(page_type)` is the **legacy** static-page reader (seven fixed page types). `read_wiki_page` / `list_wiki_pages` / `read_wiki_module` / `read_wiki_section` / `get_wiki_graph` are the **redesigned slug-keyed** surface (per-page identity, structured module payloads, cross-link graph). Prefer the slug-keyed tools for new integrations; call `list_wiki_pages` first to discover slugs.

### Discovery (3 tools)

Tools for exploring your accessible team topology. All are instant (single store/metadata read).

#### `whoami()`
Return the authenticated principal id, the connection ids you can access, and the deployed server version. Call this first to verify auth and obtain connection ids. Instant; response is stable within a session, so do not poll it.

**Returns:** `{principal_id, connections: [connection_id, ...], server_version}` — e.g. `{"principal_id": "mcp:9f2a1c…", "connections": ["conn_abc123"], "server_version": "0.1.0"}`. The `connections` list is ids only — call `list_connections` for full metadata.

#### `list_connections()`
List all platform connections (Slack workspaces, Discord servers, Teams orgs, file imports) owned by your principal, with full metadata. Results are filtered by ownership. Instant.

**Returns:** `{connections: [{connection_id, platform, display_name, status, last_synced_at, selected_channel_count, source}, ...]}`

**Field caveat:** `selected_channel_count` is the size of the user's **sync pick-list**, NOT the number of channels on the platform. `0` does not mean the connection is empty. Always call `list_channels` for the real channel list.

#### `list_channels(connection_id)`
List the channels the bot can actually read on a connection — the **ground truth** for what channels exist. Returns picked channels when the user has a non-empty sync pick-list, otherwise every channel the bot is a member of (file-import connections return every uploaded file). Instant.

**Parameters:**
- `connection_id` (required) — the connection id from `list_connections`, e.g. `"conn_abc123"`.

**Returns:** `{channels: [{channel_id, name, platform, last_sync_ts, sync_status, message_count_estimate}, ...]}`. `sync_status="never_synced"` is normal and does not mean the channel is inaccessible — call `trigger_sync(channel_id)` to index it.

**Error:** `{"error": "connection_access_denied", "connection_id": "..."}` if you do not own the connection (existence is not leaked).

### Retrieval (17 tools)

Tools for searching and reading team knowledge. Unless noted, all are read-only and instant-to-fast.

#### `ask_channel(channel_id, question, mode="deep", session_id=None)`
Flagship retrieval tool: answer a natural-language question about a channel by running the full ADK QA pipeline (BM25 + vector hybrid search + graph context + optional multi-hop reasoning) and returning a structured, cited answer. Streams thinking and tool-call events as progress notifications while it runs. Use when you need a synthesized answer; prefer `search_channel_facts`/`find_facts` for raw fact lookup, and `search_memory` when you don't yet know which channel holds the answer.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`, e.g. `"C1234567890"`.
- `question` (required) — natural-language question, **1–4000 characters** (over-length returns `invalid_parameter`), e.g. `"How does JWT auth work here?"`.
- `mode` — `"quick"` (BM25-only, no reasoning, ~3s), `"deep"` (full ADK pipeline + graph, ~20–60s; **default**), or `"summarize"` (structured summary over wiki pages, ~10–30s). Invalid values return `invalid_parameter`.
- `session_id` — conversation-continuity id from `start_new_session`; omit to use a per-principal session. Example: `"mcp:9f2a1c…:1a2b3c4d"`.

**Returns:** `{answer, citations, follow_ups, metadata}`. **Latency:** long-running (up to 90s); progress notifications are emitted during the run.

**Errors:** `{"error": "channel_access_denied", "channel_id": "..."}`, `{"error": "answer_timeout"}` if the 90s hard cap is exceeded, `{"error": "adk_error"}` on internal failure, `{"error": "invalid_parameter", "parameter": ...}`.

#### `search_channel_facts(channel_id, query, time_scope="any", limit=10)`
BM25 + vector hybrid search over atomic facts in a single channel. Returns ranked facts with citations. Faster and more precise than `ask_channel` for lookup; for substring/deterministic matching use `find_facts`; for cross-channel search use `search_memory`.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `query` (required) — search query, e.g. `"JWT token expiry"`.
- `time_scope` — `"any"` (all time; **default**) or `"recent"` (last 30 days).
- `limit` — max facts, **1–50, default 10** (clamped server-side).

**Returns:** `{facts: [{text, author, timestamp, permalink, channel_id, confidence, topic_tags}, ...]}`

**Error:** `{"error": "channel_access_denied", "channel_id": "..."}`.

#### `get_wiki_page(channel_id, page_type="overview")`
**(Legacy static-page surface.)** Fetch one pre-compiled wiki page from the fixed seven-page namespace. Faster than `ask_channel` for structured summaries. For the redesigned slug-keyed wiki, use `read_wiki_page` / `list_wiki_pages` instead.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `page_type` — one of `"overview"` (default), `"faq"`, `"decisions"`, `"people"`, `"glossary"`, `"activity"`, `"topics"`.

**Returns:** the page dict (`page_type`, `channel_id`, `content`, `summary`, `text`). `content` is `null` when the page has not been generated yet. **Error:** `{"error": "channel_access_denied", ...}`.

#### `get_recent_activity(channel_id, days=7, topic=None, limit=20)`
Return the most recent facts from a channel, newest first, optionally narrowed to a topic and time window. Use for "what happened recently in #channel?"; use `search_channel_facts` for non-time-bounded search and `ask_channel` for synthesis.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `days` — look-back window, **1–90, default 7** (clamped server-side), e.g. `14`.
- `topic` — optional topic filter, e.g. `"authentication"`.
- `limit` — max items, **1–50, default 20** (clamped server-side).

**Returns:** `{activity: [{text, author, timestamp, channel_id, topic_tags, fact_id}, ...]}`. **Error:** `{"error": "channel_access_denied", ...}`.

#### `search_media_references(channel_id, query, media_type=None, limit=5)`
Find messages containing images, PDFs, or links shared in a channel — useful for design specs, diagrams, or documentation URLs. Do not use for general knowledge search (use `search_channel_facts`).

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `query` (required) — search term, e.g. `"architecture diagram"`.
- `media_type` — `"image"`, `"pdf"`, `"link"`, or `null` (all types; default).
- `limit` — max results, **1–20, default 5** (clamped server-side).

**Returns:** `{media: [{text, media_urls, link_urls, link_titles, author, timestamp, media_type, fact_id}, ...]}`. **Error:** `{"error": "channel_access_denied", ...}`.

#### `search_memory(query, scope="all", limit=20)`
**Cross-channel** recall: hybrid (BM25 + vector) search merged across every channel the principal can access, re-ranked by score. Use when you do NOT yet know which channel holds the answer. With `scope="channel:<id>"` it searches a single channel (equivalent results to `search_channel_facts`, in this tool's response shape). Per-channel access is enforced.

**Parameters:**
- `query` (required) — search query, **1–4000 characters** (over-length returns `invalid_parameter`), e.g. `"deployment runbook"`.
- `scope` — `"all"` (every accessible channel; **default**) or `"channel:<channel_id>"`, e.g. `"channel:C1234567890"`.
- `limit` — max merged hits, **1–50, default 20** (clamped server-side).

**Returns:** `{hits: [{fact_id, text, score, channel_id, cluster_id, entity_tags}, ...], query}`. **Error:** `{"error": "channel_access_denied", "channel_id": ...}` only for an explicit unreachable `channel:<id>` scope (inaccessible channels are silently skipped under `scope="all"`).

#### `lint_wiki(channel_id, target_lang=None, run_coherence_check=True)`
Run wiki health checks for a channel and return findings (orphan pages, stale/duplicate sections, intra-page coherence issues). Use when auditing wiki quality before or after a refresh; not part of normal retrieval. **Cost note:** with `run_coherence_check=True` this makes one LLM call per page, so it is slower and more expensive than a plain read — set it `False` to skip the coherence pass.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `target_lang` — BCP-47 language tag to lint; defaults to the channel's primary (`"en"`).
- `run_coherence_check` — run the bounded LLM coherence pass, **default `True`**.

**Returns:** `{findings: [{severity, category, page_id, section_id, message, suggested_action}, ...], pages_scanned}`. **Errors:** `{"error": "channel_access_denied", ...}`, `{"error": "lint_failed"}` on internal failure.

#### `get_extraction_status(channel_id)`
Return per-status counts for the channel's background extraction queue: how many messages are still pending vs. being extracted vs. done vs. failed. Use to check whether ingestion has finished before relying on retrieval results. (Distinct from `get_job_status`, which tracks one specific sync/refresh job by id.)

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.

**Returns:** `{channel_id, counts: {pending, extracting, done, failed}, total}`. A non-zero `pending`/`extracting` count means retrieval may be incomplete; all-`done` means the channel is fully indexed. **Errors:** `{"error": "channel_access_denied", ...}`, `{"error": "extraction_status_failed"}`.

#### `read_wiki_page(channel_id, slug, target_lang="en")`
**(Redesigned slug-keyed surface.)** Return the full structured payload for one wiki page identified by slug. Distinct from the legacy `get_wiki_page(page_type)` — this exposes per-page identity and structured modules. Call `list_wiki_pages` first to discover slugs.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `slug` (required) — stable human-readable page id, e.g. `"auth-architecture"`.
- `target_lang` — BCP-47 tag, default `"en"`.

**Returns:** the WikiPage document (`content_md`, `kind`, `kind_schema`, `cross_links`, `cross_links_broken`, `pin_state`, `last_updated`). Hidden pages are excluded unless the token carries the `read:hidden_pages` scope. **Errors:** `{"error": "wiki_page_not_found", "slug": ...}`, `{"error": "channel_access_denied", ...}`.

#### `list_wiki_pages(channel_id, kind=None, scope="human", target_lang="en")`
List wiki-page summaries for a channel (slug-keyed surface). **Recommended first call** before `read_wiki_page` to discover slugs. Bodies are omitted to keep the payload small.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `kind` — optional filter, one of `"topic"`, `"entity"`, `"decisions"`, `"faq"`, `"action_items"`.
- `scope` — `"human"` (excludes hidden + merged pages; **default**) or `"all"` (requires the `read:hidden_pages` token scope, otherwise silently downgraded to `"human"`).
- `target_lang` — BCP-47 tag, default `"en"`.

**Returns:** `{channel_id, target_lang, scope, pages: [{slug, title, kind, version, last_updated, pinned, hidden}, ...]}`. **Error:** `{"error": "wiki_list_failed"}` on internal failure (and `channel_access_denied` on ACL denial).

#### `get_wiki_graph(channel_id)`
Return the channel's **wiki cross-link graph** (page-to-page links) in Cytoscape format. Distinct from `search_relationships`, which traverses the **knowledge graph** of entities/people — this is the graph of wiki pages and their links.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.

**Returns:** `{channel_id, nodes: [{data: {id, label, kind, ...}}], edges: [{data: {id, source, target, kind}}]}`. Returns empty `nodes`/`edges` when the graph backend is unavailable. **Error:** `{"error": "channel_access_denied", ...}`.

#### `read_wiki_module(channel_id, page_slug, anchor, target_lang="en")`
Fetch one module's structured `data` payload from a wiki page without downloading the whole page — token-efficient when you only need one slice. Discover valid anchors by reading the page first via `read_wiki_page` (the slug-keyed surface).

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `page_slug` (required) — slug of the host page, e.g. `"auth-architecture"`.
- `anchor` (required) — stable in-page module id, e.g. `"key-facts"`, `"decision-banner"`, `"tension-callout"`.
- `target_lang` — BCP-47 tag, default `"en"`.

**Returns:** `{channel_id, page_slug, anchor, module_id, data}`. **Errors:** `{"error": "wiki_page_not_found", ...}`, `{"error": "module_not_found", "slug": ..., "anchor": ...}`, `{"error": "channel_access_denied", ...}`.

#### `find_decisions(channel_id, since=None, author=None, limit=50)`
Find every decision recorded in a channel's wiki/fact store, sorted newest-first. Prefer this over `find_facts(fact_type="decision")` when you also need rationale and alternatives on each result. (For the SUPERSEDES-chain timeline of how one decision evolved, use `trace_decision_history` instead.) **Returns a JSON list (not a dict);** on access denial it returns an empty list `[]` rather than an error object.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `since` — optional ISO-8601 date prefix; only decisions on/after this date, e.g. `"2026-04-01"`.
- `author` — optional exact-match author name, e.g. `"Alice Chen"`.
- `limit` — max decisions, **1–100, default 50** (clamped server-side).

**Returns:** `[{fact_id, decision, decided_by, decided_at, rationale, alternatives_rejected, page_slug}, ...]` (`rationale` may be `null` when not yet extracted).

#### `get_tensions(channel_id, status=None)`
List unresolved tensions (competing positions) surfaced from `tension_callout` modules across the channel's wiki. **Returns a JSON list (not a dict);** on access denial it returns `[]`. **Note:** tension detection is not yet shipped on this track, so this is currently empty for most channels — the wiring is in place so the same call returns real data once detection lands, with no API change.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `status` — optional filter, one of `"open"`, `"blocked"`, `"deferred"`.

**Returns:** `[{tension_id, title, status, since, positions: [{author, stance, fact_id}, ...], page_slug}, ...]`.

#### `find_facts(channel_id, query, fact_type=None, limit=20)`
Deterministic **case-insensitive substring** search over fact text in a channel — not a ranked retriever. Use when you want raw fact rows that mention a keyword and `ask_channel` would over-synthesize; for semantic/vector ranking use `search_channel_facts`. **Returns a JSON list (not a dict);** on access denial it returns `[]`.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `query` (required) — substring to match in `memory_text`, e.g. `"rate limit"`.
- `fact_type` — optional filter, one of `"decision"`, `"observation"`, `"opinion"`, `"question"`, `"action_item"`.
- `limit` — max facts, **1–100, default 20** (clamped server-side).

**Returns:** `[{fact_id, memory_text, fact_type, importance, author_name, message_ts, page_slug}, ...]`.

#### `read_wiki_section(channel_id, page_slug, anchor, target_lang="en")`
Fetch ONE narrative section's structured data from a wiki page without loading the full page — token-efficient when you know the section anchor. Use `read_wiki_module` for a module's structured payload instead, and `find_facts` for cross-page fact-text search.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `page_slug` (required) — slug of the host page, e.g. `"auth-architecture"`.
- `anchor` (required) — kebab-case section id, e.g. `"context"`, `"alternatives"`, `"implications"`.
- `target_lang` — BCP-47 tag, default `"en"`.

**Returns:** `{anchor, heading, paragraphs, citations, visual, page_slug, page_title, channel_id}`. **Errors:** `{"error": "page_not_found", ...}`, `{"error": "section_not_found", "available_anchors": [...]}`, `{"error": "narrative_not_available", "has_modules": bool, "suggestion": ...}` (retry with `read_wiki_page`), `{"error": "channel_access_denied", ...}`.

#### `read_provenance(fact_id)`
Close the audit loop: given a `fact_id` surfaced by another tool (`find_decisions`, `find_facts`, `ask_channel`, …), return the original source message it was extracted from. Does not fail hard if the raw message is unreachable — `raw_message` is empty in that case but all citation fields are still populated.

**Parameters:**
- `fact_id` (required) — the fact id to resolve, e.g. `"fact_7d3a…"`.

**Returns:** `{fact_id, memory_text, source: {platform, message_id, url, author, ts}, raw_message}`. **Error:** `{"error": "fact_not_found", "fact_id": ...}` — note that access-denied is deliberately mapped to `fact_not_found` so cross-tenant fact existence is not disclosed.

### Graph (3 tools)

Tools for exploring the entity knowledge graph and decision history. Fast graph reads.

#### `find_experts(channel_id, topic, limit=5)`
Identify the most knowledgeable people about a topic in a channel, ranked by graph-edge frequency. Use to answer "who knows the most about X?"; use `search_channel_facts` to find facts rather than people.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `topic` (required) — topic/keyword, e.g. `"database indexing"`.
- `limit` — max experts, **1–20, default 5** (clamped server-side).

**Returns:** `{experts: [{handle, expertise_score, fact_count, top_topics}, ...]}` (`expertise_score` is a relative ranking weight, higher = stronger). **Error:** `{"error": "channel_access_denied", ...}`.

#### `search_relationships(channel_id, entities, hops=2)`
Traverse the **knowledge graph** of entities (people, projects, decisions) to find how they connect. Distinct from `get_wiki_graph` (page-link graph) — this returns entity nodes and relationship edges. Use to answer "how is X related to Y?".

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `entities` (required) — list of entity names to start from, e.g. `["Alice", "ProjectX"]`.
- `hops` — traversal depth, **1–4, default 2** (clamped server-side).

**Returns:** `{nodes: [{name, type}, ...], edges: [{source, target, type, confidence, context}, ...], text, entities_searched}`. **Error:** `{"error": "channel_access_denied", ...}`.

#### `trace_decision_history(channel_id, topic)`
Reconstruct the decision timeline for a topic by following `SUPERSEDES` edges in the knowledge graph — which earlier decisions were overridden and by what. Distinct from `find_decisions` (flat wiki/fact-store decision list): use this for the historical chain, `find_decisions` for the current decision set with rationale.

**Parameters:**
- `channel_id` (required) — target channel from `list_channels`.
- `topic` (required) — decision topic, e.g. `"database choice"`.

**Returns:** `{decisions: [{entity, superseded_by, relationship, confidence, context, position}, ...]}`. **Error:** `{"error": "channel_access_denied", ...}`.

### Session (1 tool)

#### `start_new_session()`
Reset the conversation thread and obtain a fresh `session_id` to pass to `ask_channel` — use it to drop prior context when switching topics or starting an unrelated inquiry. Do not call before every question. Instant.

**Returns:** `{session_id}` — e.g. `{"session_id": "mcp:9f2a1c…:1a2b3c4d"}`. The id is an opaque conversation-boundary marker accepted by `ask_channel(session_id=...)`.

### Orchestration (3 tools)

Long-running operations that return immediately with a `job_id` for polling. Rate-limited to 5/min per principal for the two write tools.

#### `trigger_sync(channel_id, sync_type="incremental", connection_id=None)`
Trigger an incremental or full message sync of a channel into the knowledge base. Returns a job envelope within ~5s; ingestion runs in the background — poll `get_job_status` or read `atlas://job/<job_id>`. **Idempotent:** an existing queued/running job for the same channel is returned rather than creating a duplicate. **When to use:** only when the user explicitly asks to sync, OR when retrieval is empty/stale and the channel's `last_sync_ts` is older than 24h — sync is expensive and rate-limited. Do not call as a precaution before every question.

**Parameters:**
- `channel_id` (required) — channel to sync from `list_channels`, e.g. `"C1234567890"`.
- `sync_type` — `"incremental"` (only new messages; **default**), `"full"` (re-fetch everything; expensive), or `"auto"` (server chooses).
- `connection_id` — optional but strongly recommended when the principal owns multiple same-platform connections, to avoid mis-routing, e.g. `"conn_abc123"`.

**Returns:** `{job_id, status_uri: "atlas://job/{job_id}", status: "queued"}`. **Errors:** `{"error": "channel_access_denied", ...}`, `{"error": "cooldown_active", "retry_after_seconds": N}`, `{"error": "service_unavailable", "service": ...}` (e.g. the platform bridge is unreachable), `{"error": "internal_error", ...}`.

#### `refresh_wiki(channel_id, page_types=None)`
Regenerate pre-compiled wiki pages for a channel from its ingested facts. Returns a job envelope within ~5s; generation runs in the background. **Expensive** — only call after a fresh sync added new facts or when the user explicitly requests regeneration; wiki pages are normally rebuilt automatically by the sync pipeline.

**Parameters:**
- `channel_id` (required) — channel from `list_channels`, e.g. `"C1234567890"`.
- `page_types` — optional subset to regenerate from `"overview"`, `"faq"`, `"decisions"`, `"people"`, `"glossary"`, `"activity"`, `"topics"`; omit to regenerate all.

**Returns:** `{job_id, status_uri: "atlas://job/{job_id}", status: "queued"}`. **Errors:** `{"error": "channel_access_denied", ...}`, `{"error": "cooldown_active", "retry_after_seconds": N}`, `{"error": "service_unavailable", "service": ...}`, `{"error": "internal_error", ...}`.

#### `get_job_status(job_id)`
Poll the state of a long-running job created by `trigger_sync` or `refresh_wiki`. **Polling guidance:** wait a few seconds between polls and back off — do not hot-loop. The `atlas://job/<id>` resource read returns the identical payload for clients that prefer `resources/read`.

**Parameters:**
- `job_id` (required) — the id returned by `trigger_sync` / `refresh_wiki`, e.g. `"job_3f1c9a"`.

**Returns:** `{job_id, kind, status, progress, started_at, updated_at, ended_at, result, error, target}`. `status` ∈ `queued` | `running` | `done` | `error` | `cancelled`; `progress` is `0.0–1.0` or `null` when unavailable. **Error:** `{"error": "job_not_found", "job_id": ...}` if the job does not exist OR is owned by another principal (ownership is not disclosed).

### Deprecation Shim (1 tool)

#### `search_channel_knowledge(channel_id="", query="")` (DEPRECATED)
Retired tool name retained as a shim. It performs no retrieval and always returns a structured `tool_renamed` error pointing at the replacements, so migrating callers get a clear signal instead of a silent failure.

**Returns:** `{"error": "tool_renamed", "replacement": ["ask_channel", "search_channel_facts"], "detail": "..."}`

**Migration:** replace `search_channel_knowledge(channel_id, query)` with:
- `ask_channel(channel_id, query, mode="quick")` for natural-language, cited answers, or
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
- **`adk_error`** — `ask_channel` hit an internal QA-pipeline failure. Details are logged server-side, never returned.
- **`service_unavailable`** — a backing service (e.g. the platform bridge for Slack/Discord/Teams) is unreachable. Returned by `trigger_sync` / `refresh_wiki`; the error includes a `service` field.
- **`fact_not_found`** — `read_provenance` could not resolve the `fact_id` (the fact is unknown OR the caller lacks access — the two are deliberately indistinguishable to avoid leaking cross-tenant existence).
- **`wiki_page_not_found`** / **`module_not_found`** / **`page_not_found`** / **`section_not_found`** / **`narrative_not_available`** — returned by the slug-keyed wiki tools when the requested slug, module anchor, or narrative section does not exist.
- **`lint_failed`** / **`extraction_status_failed`** / **`internal_error`** — generic internal-failure codes returned by the corresponding tools when an underlying operation fails.

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

- **Read-mostly surface:** External agents cannot mutate knowledge — there are no tools to upsert facts, delete memories, or edit wiki pages. The only state-changing tools are `trigger_sync` and `refresh_wiki`, which queue background jobs; all other tools are read-only. Direct write operations remain dashboard-only.
- **Tension detection not yet shipped:** `get_tensions` is wired end-to-end but returns an empty list for most channels until tension detection lands; the API will not change when it does.
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
- [Hackathon Builder Guide](guides/hackathon-builder-guide.md) — concrete tool-by-tool starter specs for common things to build with these MCP tools.
