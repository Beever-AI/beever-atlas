## Why

The ingestion pipeline (raw Slack message -> atomic facts in Weaviate + graph entities in Neo4j) has several semantic and structural gaps that prevent it from serving as a reliable enterprise knowledge base. Key issues: (1) entity deduplication is string-similarity only — "Atlas" and "Beever Atlas" are treated as different nodes, (2) no coreference resolution — pronouns like "it" and "they" lose their referents, (3) multimodal coverage is shallow — videos, Office docs, and audio are ignored, (4) vector embeddings are stored but never queried for semantic search, (5) no temporal fact lifecycle — contradictory facts coexist indefinitely, and (6) cross-batch thread context is lost. For an enterprise product these gaps erode trust in search results and graph accuracy.

## What Changes

- **Coreference resolution layer**: Add a pre-extraction pass that resolves pronouns and implicit references ("it", "they", "this", "that project") to their antecedents within the conversation window, producing enriched text for downstream extractors.
- **Semantic entity deduplication**: Supplement Jaro-Winkler string matching with embedding-based similarity so "Atlas", "Beever Atlas", and "the atlas project" merge into one canonical node.
- **Multimodal expansion**: Add extractors for video (keyframe + audio transcript), Office documents (docx/xlsx/pptx text extraction), and audio files; feed extracted content into the same fact/entity pipeline.
- **Semantic vector search**: Activate Weaviate near-vector queries for fact retrieval so the query layer can find semantically similar facts, not just exact field matches.
- **Temporal fact lifecycle**: Implement fact supersession — when a new fact contradicts an existing one, mark the old fact as invalidated with a pointer to its replacement.
- **Cross-batch thread context**: Persist parent message summaries so threaded replies that span ingestion batches retain their conversational context.
- **Smarter orphan handling**: Replace hard-delete of relationship-less entities with a soft "pending" state that survives across a configurable batch window before final pruning.

## Capabilities

### New Capabilities
- `coreference-resolution`: Pre-extraction pass resolving pronouns and implicit references to named entities within conversation context
- `semantic-entity-dedup`: Embedding-based entity merging to complement string-similarity deduplication in the cross-batch validator
- `multimodal-expansion`: Extraction support for video (keyframes + transcript), Office docs (docx/xlsx/pptx), and audio files
- `semantic-search`: Activate Weaviate near-vector queries for semantic fact retrieval
- `temporal-fact-lifecycle`: Fact supersession, invalidation, and contradiction detection across ingestion batches
- `cross-batch-thread-context`: Persistent parent message summaries for threaded replies spanning multiple ingestion batches
- `soft-orphan-handling`: Grace-period entity retention replacing immediate orphan deletion

### Modified Capabilities

## Impact

- **Agents**: `preprocessor.py` (coreference pass, media expansion, thread context lookup), `entity_extractor.py` (semantic dedup candidates in prompt), `cross_batch_validator.py` (embedding similarity merge, soft orphan logic), `fact_extractor.py` (contradiction detection hints)
- **Services**: `media_processor.py` (new extractors for video/audio/office), new `coreference_resolver.py` service
- **Stores**: `weaviate_store.py` (near-vector search, fact invalidation fields), `neo4j_store.py` (soft-delete/pending state on Entity nodes, supersession edges), `entity_registry.py` (embedding-based fuzzy match)
- **Dependencies**: New libraries for video processing (e.g., `moviepy` or `ffmpeg`), Office extraction (`python-docx`, `openpyxl`, `python-pptx`), speech-to-text API for audio
- **Prompts**: Updated extraction prompts for coreference-enriched input, contradiction detection instructions, multimodal content handling
- **API**: New semantic search endpoint or updated query router to use vector similarity
