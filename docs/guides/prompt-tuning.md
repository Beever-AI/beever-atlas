# Prompt Tuning Guide

## Overview

The dry-run CLI lets you test ingestion prompts without touching any database. Edit a prompt, run the script, see results in seconds — no Weaviate, Neo4j, or MongoDB writes.

## Quick Start

```bash
# First run — fetches messages from Slack bridge and caches them locally
uv run python -m beever_atlas.scripts.dry_run C0AMY9QSPB2

# Re-run with cached messages (instant, no network)
uv run python -m beever_atlas.scripts.dry_run C0AMY9QSPB2 --cached
```

## Commands

| Command | Description |
|---------|-------------|
| `--cached` | Use locally cached messages (skip bridge fetch) |
| `--facts-only` | Only run fact extraction (skip entities) |
| `--entities-only` | Only run entity extraction (skip facts) |
| `--limit N` | Process only the first N messages |
| `--batch-size N` | Messages per batch (default: 5) |

## Iteration Workflow

```
1. Cache messages (once)
   uv run python -m beever_atlas.scripts.dry_run <CHANNEL_ID>

2. Edit a prompt
   vim src/beever_atlas/agents/prompts/fact_extractor.py

3. Test instantly
   uv run python -m beever_atlas.scripts.dry_run <CHANNEL_ID> --cached --facts-only

4. Compare output, repeat steps 2-3

5. When satisfied, do a full sync via the UI to populate databases
```

## Output Format

The script prints color-coded results per batch:

```
============================================================
  Batch 1/3 (5 messages)
============================================================
  [kai.y.yang] Hi everyone, my name is Alan...
  [kai.y.yang] I'm going to study the Beever Atlas codebase...

  Facts extracted: 2 (3.1s)
    [0.85|high] Alan is studying the Beever Atlas codebase and exploring Claude Code tools as part of onboarding
    [0.30|low]  Alan introduced himself to the channel

  Entities: 3 | Relationships: 2 (2.8s)
    [Person] Alan (aka kai.y.yang)
    [Project] Beever Atlas
    [Technology] Claude Code
     Alan --[USES]--> Claude Code (conf=0.9)
     Alan --[WORKS_ON]--> Beever Atlas (conf=0.8)
```

## File Locations

| File | Purpose |
|------|---------|
| `.omc/cache/messages-<channel>.json` | Cached raw messages from bridge |
| `.omc/cache/dry-run-<channel>.json` | Full extraction results (JSON) |
| `src/beever_atlas/agents/prompts/fact_extractor.py` | Fact extraction prompt |
| `src/beever_atlas/agents/prompts/entity_extractor.py` | Entity extraction prompt |
| `src/beever_atlas/agents/prompts/classifier.py` | Classification prompt |
| `src/beever_atlas/agents/prompts/cross_batch_validator.py` | Cross-batch validation prompt |

## Prompt Files

### Fact Extractor (`fact_extractor.py`)
Controls what facts are extracted and how they're written. Key levers:
- **Quality bar**: "Would a new team member find this valuable 6 months from now?"
- **Style**: Concise human insight, no raw IDs or timestamps
- **Skip criteria**: Greetings, reactions, acknowledgments
- **Scoring**: specificity x actionability x verifiability (0.0-1.0)

### Entity Extractor (`entity_extractor.py`)
Controls what entities and relationships are found. Key levers:
- **Relationship requirement**: Every entity must have at least one relationship
- **Entity types**: Person, Decision, Project, Technology, Team, Meeting, Artifact
- **Confidence levels**: explicit=1.0, strong_context=0.8, implied=0.5
- **Alias resolution**: Merge `kai.y.yang` and `Alan` into one entity

### Classifier (`classifier.py`)
Enriches facts with topic tags and importance. Key levers:
- **Importance**: critical / high / medium / low
- **Topic vocabulary**: 22 canonical tags (architecture, deployment, security, etc.)
- **Tag count**: 1-3 per fact

### Cross-Batch Validator (`cross_batch_validator.py`)
Deduplicates entities across batches. Key levers:
- **Orphan filtering**: Remove entities with zero relationships
- **Alias merging**: Collapse duplicate entities across batches
- **Contradiction detection**: Flag conflicting relationships

## Full Sync Testing (with Database Reset)

After you're happy with the dry-run output, test the full pipeline end-to-end:

### Step 1: Reset databases

```bash
# Delete Weaviate MemoryFact collection (atomic facts / vector store)
python -c "
import weaviate
client = weaviate.connect_to_local()
client.collections.delete('MemoryFact')
client.close()
print('Weaviate: MemoryFact collection deleted')
"

# Delete Neo4j entities and relationships (knowledge graph)
python -c "
import asyncio
from neo4j import AsyncGraphDatabase
driver = AsyncGraphDatabase.driver('bolt://localhost:7687', auth=('neo4j', 'beever_atlas_dev'))
async def clear():
    async with driver.session() as s:
        await s.run('MATCH (n) DETACH DELETE n')
    await driver.close()
    print('Neo4j: all nodes and relationships deleted')
asyncio.run(clear())
"

# Delete MongoDB sync state (so next sync is a full sync, not incremental)
python -c "
from pymongo import MongoClient
client = MongoClient('mongodb://localhost:27017')
db = client['beever_atlas']
db['sync_jobs'].delete_many({})
db['channel_sync_state'].delete_many({})
db['write_intents'].delete_many({})
db['activity_events'].delete_many({})
client.close()
print('MongoDB: sync state cleared')
"
```

Or as a single one-liner:

```bash
python -c "
import weaviate, asyncio
from pymongo import MongoClient
from neo4j import AsyncGraphDatabase

# Weaviate
wc = weaviate.connect_to_local()
if wc.collections.exists('MemoryFact'):
    wc.collections.delete('MemoryFact')
wc.close()
print('Weaviate cleared')

# Neo4j
async def clear_neo4j():
    d = AsyncGraphDatabase.driver('bolt://localhost:7687', auth=('neo4j', 'beever_atlas_dev'))
    async with d.session() as s:
        r = await s.run('MATCH (n) DETACH DELETE n')
        summary = await r.consume()
        print(f'Neo4j cleared ({summary.counters.nodes_deleted} nodes)')
    await d.close()
asyncio.run(clear_neo4j())

# MongoDB
mc = MongoClient('mongodb://localhost:27017')
db = mc['beever_atlas']
for col in ['sync_jobs', 'channel_sync_state', 'write_intents', 'activity_events']:
    db[col].delete_many({})
mc.close()
print('MongoDB cleared')
"
```

### Step 2: Restart the server

```bash
# If already running with --reload, just save a file to trigger restart.
# Otherwise:
uv run uvicorn beever_atlas.server.app:app --reload --port 8000
```

### Step 3: Sync via the UI

1. Open `http://localhost:5173/channels/<CHANNEL_ID>`
2. Click **Sync Channel**
3. The first sync after a reset will always be a **full sync** (fetches all messages)
4. Watch the progress bar — with `?dev=true` in the URL you'll see the dev overlay

### Step 4: Check results

- **Memories tab**: Should show extracted facts as concise human insights
- **Graph tab**: Should show entities with connecting relationships
- **Terminal logs**: Structured JSON, filterable by `grep '"cat":"llm"'`

### Quick reset + sync cycle

```bash
# Reset all DBs, restart server, then sync via API:
python -c "import weaviate; c=weaviate.connect_to_local(); c.collections.delete('MemoryFact'); c.close()" && \
curl -X POST http://localhost:8000/api/channels/<CHANNEL_ID>/sync?sync_type=full
```

## Tips

- Use `--limit 2 --batch-size 2` for the fastest dry-run iteration (1 batch, 2 messages)
- Use `--facts-only` or `--entities-only` to isolate which prompt you're tuning
- Check `.omc/cache/dry-run-<channel>.json` for the full structured output
- The quality gate callbacks still run in dry-run mode, so you'll see the same filtering as production
- You don't need to reset Neo4j/MongoDB for every prompt test — only reset when you want a clean slate for the full sync
- The Weaviate collection is auto-recreated on server startup, so just deleting it is enough
