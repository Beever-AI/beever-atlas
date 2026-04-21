# demo/fixtures — Pre-computed Seed Fixtures

This directory contains pre-computed fixtures for the Beever Atlas demo workspace.
They are loaded by `demo/seed.py --precomputed` (the default, invoked via `make demo`).

**Current state:** stub placeholders. A maintainer must run `make demo-regenerate-fixtures`
once before the PR lands to populate real fixtures. See below.

---

## Files

| File | Format | Description |
|------|--------|-------------|
| `manifest.json` | JSON | Model versions, generation date, embedding dimensions, corpus file list. |
| `weaviate_facts.jsonl` | JSONL (binary in git) | One fact object per line: `{text, source_id, channel_id, embedding: [2048 floats], metadata}`. Loaded into the Weaviate `Fact` collection with pre-computed vectors. |
| `neo4j_graph.cypher` | Cypher + JSON comments | Entity nodes and relationships as parameterised Cypher blocks. Each block is a `// params: {...}` comment followed by a `MERGE`/`MATCH` statement. |
| `mongo_seed.json` | JSON | Channel document, `channel_sync_state` document, and message documents for MongoDB. |

---

## Regenerating Fixtures

Fixtures must be regenerated when:
- Corpus files in `demo/corpus/` change.
- `JINA_DIMENSIONS` changes (the manifest records this and `seed.py` aborts with an error on mismatch).
- The embedding model changes.

To regenerate:

```bash
# Requires GOOGLE_API_KEY and JINA_API_KEY in .env
make demo-regenerate-fixtures
```

This runs `seed.py --live --write-fixtures` inside Docker (using the base Dockerfile),
ingests all `demo/corpus/*.md` files through the full ADK pipeline, and overwrites the
fixture files in this directory. Commit the resulting files.

---

## Fixture Format Details

### `manifest.json`

```json
{
  "generated_at": "<ISO-8601 timestamp>",
  "embedding_model": "jina-embeddings-v3",
  "llm_model": "gemini-2.5-flash",
  "jina_dimensions": 2048,
  "corpus_files": ["ada-lovelace.md", "..."],
  "note": "..."
}
```

The loader checks `jina_dimensions` against the current `JINA_DIMENSIONS` env var and aborts
with a clear error if they do not match.

### `weaviate_facts.jsonl`

One JSON object per line (no trailing commas, no wrapping array). Each object:

```json
{"text": "Ada Lovelace was an English mathematician...", "source_id": "demo-msg-0001", "channel_id": "demo-wikipedia", "embedding": [0.123, -0.456, ...], "metadata": {"source_file": "ada-lovelace.md"}}
```

The `embedding` array must have exactly `jina_dimensions` floats (default 2048).

This file is tracked as binary in `.gitattributes` to avoid diff noise from the large vectors.

### `neo4j_graph.cypher`

```cypher
// params: {"name": "Ada Lovelace", "desc": "English mathematician (1815-1852)", "channel_id": "demo-wikipedia"}
MERGE (p:Person {name: $name}) SET p.description = $desc, p.channel_id = $channel_id;

// params: {"src": "Ada Lovelace", "tgt": "Charles Babbage"}
MATCH (a {name: $src}), (b {name: $tgt}) MERGE (a)-[:COLLABORATED_WITH]->(b);
```

### `mongo_seed.json`

```json
{
  "channels": [{"channel_id": "demo-wikipedia", "name": "#demo", ...}],
  "channel_sync_state": [{"channel_id": "demo-wikipedia", "status": "completed", ...}],
  "messages": [{"message_id": "demo-msg-0001", "channel_id": "demo-wikipedia", ...}]
}
```
