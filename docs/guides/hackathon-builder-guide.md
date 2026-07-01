# Hackathon Builder Guide — Build on Beever Atlas via MCP

Starter specs for the "build on Beever Atlas" use cases. Each one lists the exact tools to call, a bar for a working demo, and a stretch goal — so you have a running start instead of a blank page.

## Prerequisites

- Docker + Docker Compose running locally (`make demo` or `./atlas` — see the [Quick Start](../../README.md#-quick-start))
- `whoami` → `list_channels` to get real `channel_id`s before calling any retrieval tool
- Building against your own Slack/Discord history? Sync a channel first (dashboard: **Connections → Add Workspace → Select channels → Sync**, or the `trigger_sync` tool) — new channels take a few minutes to index
- No workspace connected yet, or just want to build/test your integration first? The pre-seeded `demo-wikipedia` channel (from `make demo`, zero API keys) works for that — point your integration at it before connecting anything real

## MCP setup (Claude Code / Cursor)

```json
{
  "mcpServers": {
    "beever-atlas": {
      "url": "http://localhost:8000/mcp",
      "transport": "streamable-http",
      "headers": { "Authorization": "Bearer ${BEEVER_MCP_KEY}" }
    }
  }
}
```

See [docs/mcp-server.md](../mcp-server.md) for generating a `BEEVER_MCP_KEY`.

---

## 1. Team-onboarding agent

**Build:** answers a new joiner's questions from past Slack/Discord history.

**Tools:**
- `ask_channel(channel_id, question, mode="deep")` — the flagship Q&A tool; returns a cited, synthesized answer
- `find_experts(channel_id, topic)` — when the answer isn't there, route to a human instead of a dead end

**Done bar:** a new joiner asks a question in your bot and gets a cited answer or an "ask @person" pointer.

**Stretch:** chain `search_relationships(channel_id, entities)` to also show how the answer connects to other systems/decisions.

## 2. "Why did we decide X" tracker

**Build:** retrieves the reasoning behind a past decision.

**Tools:**
- `find_decisions(channel_id, since?, author?)` — current decisions with rationale + rejected alternatives
- `trace_decision_history(channel_id, topic)` — the full supersession timeline ("what did we try before this")

**Done bar:** given a topic, returns the current decision + why, with an option to show the full history.

**Gotcha:** `trace_decision_history` needs a channel with revised decisions — works best on a mature/synced channel; returns an empty result (not an error) on fresh ones.

## 3. Community-support bot (Discord / Telegram)

**Build:** answers recurring questions in a community server using its own history.

**Tools:**
- `ask_channel(mode="quick")` — fast BM25-only replies (~3s) for simple questions
- `ask_channel(mode="deep")` — for anything needing reasoning across messages
- `get_recent_activity(channel_id, days)` — "what's been discussed lately"

**Done bar:** the bot replies in-thread with a cited answer instead of "let me check with the team."

## 4. Atlas + coding assistant via MCP

**Build:** wire Atlas into Claude Code/Cursor so it recalls your team's past technical choices while you code.

**Tools:** the MCP config above, then just use it naturally — `ask_channel` / `find_decisions` / `search_relationships` fire automatically when you ask the assistant "why did we pick X" mid-session.

**Done bar:** ask your coding assistant a "why does our code do X" question and it answers from real team history, not a guess.

## 5. Meeting-memory (standups → action items)

**Build:** turns standup notes into a retrievable action-item tracker.

**Tools:**
- `find_facts(channel_id, query, fact_type="action_item")` — deterministic substring filter, exact type match
- `get_recent_activity(channel_id, days=7)` — weekly recap

**Done bar:** "what are this week's open action items" returns a clean, real list.

---

## Data track — MAGIC data-agent skills

[MAGIC data-agent skills](https://github.com/Votee-AI/magic-data-agent-skills) is a different kind of tool from Atlas: not a server, but a set of installable knowledge packages for your coding assistant (Claude Code, Cursor, ...). Each skill teaches the agent domain knowledge and reference scripts; the agent then writes its own code adapted to your specific data.

**Install:**
```bash
npm install -g @votee-ai/magic-data-agent-skills
magic-data-agent-skills init --tools claude   # or cursor, windsurf, ...
pip install -r requirements.txt
```
Then just ask your agent in natural language (e.g. *"Load and profile this CSV, clean any issues, and generate a summary report"*) or invoke a slash command directly (e.g. `/magic:lifecycle`).

### 6. Data-processing pipeline (messy multi-format data → agent-usable)

**Build:** cleans/structures messy real-world data into something an agent can reliably use.

**Skills:** `magic-workspace-init` (setup) → `magic-data-loading` (auto-detects format/encoding — CSV, TSV, Parquet, JSON, JSONL, Excel, databases, HuggingFace) → `magic-data-profiling` (quality score, distributions, outliers) → `magic-data-cleaning` (fixes what profiling found) → `magic-data-transformation` (reshape/join/aggregate into the final structure). `/magic:lifecycle` orchestrates the whole sequence for you.

**Done bar:** messy input files in, one clean structured output file out.

**Gotcha:** "audio transcripts" from the two-pager's phrasing means already-transcribed text/JSON, not raw audio — `magic-data-loading`'s supported formats are file/DB/HuggingFace sources, not audio-to-text transcription itself.

### 7. Low-resource language dataset-builder (e.g. Cantonese)

**Build:** collect, clean, and structure data for an underserved language.

**Skills:** `magic-data-loading` (genuine CJK/encoding support, confirmed in the skill's own docs) → `magic-data-cleaning` → `magic-data-validation` (schema + fitness-for-use check) → `magic-data-synthesis` (LLM-based translate/fill/enrich via DataDesigner — e.g. translate an English seed set) → `magic-report-generation` / `generate_dataset_card.py` for a dataset card.

**Done bar:** a cleaned, validated dataset file plus a dataset card describing it.

**Gotcha:** `magic-data-synthesis` uses your coding assistant's own configured model, not a Cantonese-specialist model — keep scope to translating/augmenting an existing seed set rather than expecting deep Cantonese subject-matter expertise from the tool itself.

### 8. Data-storytelling for the "AI Data Desert"

**Build:** makes a specific language's data gap tangible.

**Flow:** start at [lingua.beever.ai](https://lingua.beever.ai) (reference only, not code) to pick an underserved language and find where its data lives → `magic-data-loading` (ingest whatever you found) → `magic-statistical-analysis` (quantify the gap) → `magic-data-visualization` (chart it) → `magic-report-generation` (assemble the deliverable).

**Done bar:** a short visual/report artifact making the gap for your chosen language concrete.

---

## Reference: tool cheat-sheet

| Tool | Use it for | Not for |
|---|---|---|
| `ask_channel` | A composed, cited answer to a natural-language question | Raw fact rows (use `search_channel_facts`) |
| `search_channel_facts` | Ranked hybrid (BM25 + vector) search for specific facts | An exact keyword match (use `find_facts`) |
| `find_facts` | Deterministic substring/type filter (e.g. `fact_type="action_item"`) | Semantic/ranked search |
| `find_decisions` | Current decisions + rationale | Chronological history (use `trace_decision_history`) |
| `trace_decision_history` | How a decision evolved over time (`SUPERSEDES` chain) | The current decision alone (use `find_decisions`) |
| `find_experts` | Ranking people by topic knowledge | Ranking facts |
| `search_relationships` | How named entities connect in the knowledge graph | Wiki page-link graph (use `get_wiki_graph`) |
| `get_recent_activity` | Time-bounded "what happened lately" | Unbounded search (use `search_channel_facts`) |

One correction worth flagging: `search_channel_knowledge` is a **deprecated shim** that just redirects to `ask_channel` — don't use it in new integrations.
