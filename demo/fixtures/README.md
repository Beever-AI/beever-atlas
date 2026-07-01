# demo/fixtures — Pre-computed Seed Fixtures

This directory contains pre-computed fixtures for the Beever Atlas demo workspace.
They are loaded by `demo/seed.py --precomputed` (the default, invoked via `make demo`).

Fixtures are populated from a real run of the full ADK ingestion pipeline (extraction,
consolidation, and wiki generation) against `demo/corpus/*.md` — see "Regenerating
Fixtures" below.

---

## Files

| File | Format | Description |
|------|--------|-------------|
| `manifest.json` | JSON | Model versions, generation date, embedding dimensions, corpus file list. |
| `weaviate_facts.jsonl` | JSONL (binary in git) | One fact object per line: `{text, source_id, channel_id, embedding: [2048 floats], metadata}`. Loaded into the Weaviate `MemoryFact` collection with pre-computed vectors. |
| `neo4j_graph.cypher` | Cypher + JSON comments | `Entity` (keyed by name+type), `Event` (keyed by weaviate_id), and `Media` (keyed by url) nodes, plus `MENTIONED_IN` / `REFERENCES_MEDIA` / direct Entity–Entity relationships, as parameterised Cypher blocks. Each block is a `// params: {...}` comment followed by a `MERGE`/`MATCH` statement. |
| `mongo_seed.json` | JSON | Channel document, `channel_sync_state` document, and message documents for MongoDB. Currently always empty — see note below. |
| `wiki_seed.json` | JSON | The channel's generated wiki: one `wiki_cache` document (rendered page content) and the corresponding `wiki_pages` rows (page metadata) for MongoDB. |

**Note on `mongo_seed.json`:** it's always empty (0 channels/sync-state/messages).
`demo/seed.py --live` drives ingestion directly through `BatchProcessor`, which never
writes a `channels` or `channel_sync_state` document — those come from the real sync
path (`SyncRunner`), which the demo script doesn't replicate. This is pre-existing and
harmless: nothing in the demo depends on those documents existing.

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
// params: {"name": "Ada Lovelace", "type": "Person", "scope": "global", "channel_id": null, ...}
MERGE (n:Entity {name: $name, type: $type}) SET n.scope = $scope, n.channel_id = $channel_id, ...;

// params: {"src_name": "Ada Lovelace", "src_type": "Person", "tgt_weaviate_id": "d66fad4e-..."}
MATCH (a {name: $src_name, type: $src_type}), (b {weaviate_id: $tgt_weaviate_id}) MERGE (a)-[:MENTIONED_IN]->(b);
```

Each node label uses its real MERGE key (see `demo/seed.py`'s `_export_fixtures`/`_emit_node`) —
`Entity` on `(name, type)`, `Event` on `weaviate_id`, `Media` on `url` — not a uniform `name`.
Most `Entity` nodes are `scope: "global"` with `channel_id: null`; their relevance to this
channel flows through the `MENTIONED_IN` edge to a channel-scoped `Event` node instead.

### `mongo_seed.json`

```json
{
  "channels": [],
  "channel_sync_state": [],
  "messages": []
}
```

### `wiki_seed.json`

```json
{
  "wiki_cache": [
    {"channel_id": "demo-wikipedia:en", "generated_at": "...", "pages": {"overview": {"content": "...", ...}, "..."}}
  ],
  "wiki_pages": [
    {"channel_id": "demo-wikipedia", "target_lang": "en", "page_id": "overview", "slug": "overview", "kind": "topic", ...}
  ]
}
```

`wiki_cache` holds one document per `channel_id:target_lang` with the rendered page content
(`content`, `modules`, `narrative_sections`, ...). `wiki_pages` holds one row per page with
persistence metadata (`kind`, `version`, dirty-tracking, ...) — `wiki/cache.py`'s `get_page()`
merges both at read time. Neither is written by `WikiBuilder`'s initial-build path to Neo4j —
the `:WikiPage` graph (for the separate wiki-graph visualization) is only populated by
`WikiMaintainer`'s incremental path, which the demo's one-shot seed doesn't invoke. The demo's
wiki content and pages render correctly without it; only the wiki-graph panel stays empty.
