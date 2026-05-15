## Context

Beever Atlas ingests Slack messages through a 7-stage ADK pipeline: Preprocessor -> (FactExtractor || EntityExtractor) -> Classifier -> Embedder -> CrossBatchValidator -> Persister. Facts land in Weaviate (vector store), entities/relationships in Neo4j (graph store). The system serves as an enterprise internal knowledge base.

Current state:
- Entity dedup uses Jaro-Winkler string similarity only — semantically equivalent names ("Atlas" vs "Beever Atlas") are not merged unless an explicit alias exists.
- No coreference resolution — pronouns and implicit references pass through unresolved.
- Multimodal support covers images (Gemini vision) and PDFs (text extraction) only. Video, audio, and Office docs are metadata-only.
- Weaviate stores embedding vectors but exposes no semantic search — all retrieval is field-filter based.
- No temporal fact lifecycle — contradictory facts coexist indefinitely.
- Thread context is lost across ingestion batches.
- Orphan entities (no relationships in current batch) are hard-deleted immediately.

Constraints:
- Pipeline runs on Google ADK (SequentialAgent/ParallelAgent/LlmAgent).
- LLM calls use Gemini via ADK; adding extra LLM calls impacts latency and cost.
- Weaviate and Neo4j are the only persistent stores (plus MongoDB for outbox).
- Must remain backward-compatible with existing stored facts/entities.

## Goals / Non-Goals

**Goals:**
- Resolve pronoun/implicit references before extraction so downstream agents see explicit entity names
- Merge semantically equivalent entities using embedding similarity, not just string matching
- Expand multimodal ingestion to cover video (keyframes + transcript), Office documents (docx/xlsx/pptx), and audio
- Activate Weaviate near-vector search for semantic fact retrieval
- Implement fact supersession so contradictory or outdated facts are marked invalid with pointers to replacements
- Preserve thread context across ingestion batches
- Replace hard orphan deletion with a grace-period soft state

**Non-Goals:**
- Real-time streaming ingestion (batch model is retained)
- Building a full NLP coreference model from scratch (we use LLM-based resolution)
- Supporting non-Slack platforms in this change (adapter layer stays Slack-only)
- Building a complete query/RAG layer (we only activate vector search primitives)
- OCR for handwritten or scanned documents
- Live transcription of ongoing meetings

## Decisions

### D1: LLM-based coreference resolution as a preprocessor sub-step

**Choice**: Add an LLM call in the preprocessor that takes a sliding window of recent messages (current batch + last N persisted messages from the channel) and rewrites pronoun references inline.

**Rationale**: Dedicated NLP coreference models (e.g., neuralcoref, coref-hoi) are English-only, require GPU, and struggle with domain-specific terms. An LLM call with conversation context handles multilingual, domain-specific references naturally.

**Alternatives considered**:
- *neuralcoref / spaCy pipeline*: Rejected — English-only, poor on domain jargon, extra dependency.
- *Post-extraction entity linking*: Rejected — by the time entities are extracted, the pronoun context is lost; fixing after extraction is harder than enriching before.

**Implementation**: New `CoreferenceResolver` service called by preprocessor. Takes batch messages + recent channel history (last 20 messages from MongoDB/Weaviate). Returns rewritten text with pronouns replaced by explicit entity names. Original text preserved in `raw_text` field.

### D2: Embedding-based entity dedup in CrossBatchValidator

**Choice**: Before Jaro-Winkler matching, compute embeddings for extracted entity names and compare against known entity name embeddings using cosine similarity (threshold 0.85). Candidates above threshold are presented to the LLM validator for confirmation.

**Rationale**: String similarity fails on semantic equivalence ("Beever Atlas" vs "Atlas" = 0.55 Jaro-Winkler, below 0.8 threshold). Embedding similarity captures meaning. LLM confirmation prevents false merges.

**Alternatives considered**:
- *Pure embedding similarity without LLM confirmation*: Rejected — too many false positives (e.g., "Redis" and "Redshift" embed similarly).
- *Knowledge graph link prediction*: Rejected — requires mature graph with many edges; cold-start problem.
- *Prebuilt synonym dictionary*: Rejected — doesn't scale to project-specific entities.

**Implementation**: Reuse Jina embeddings (same model as fact embeddings). Cache entity name embeddings in Neo4j `Entity.name_vector` property. CrossBatchValidator prompt updated to include embedding-similarity candidates alongside alias matches.

### D3: Modular media extractors with a registry pattern

**Choice**: Refactor `MediaProcessor` into a registry of extractors keyed by MIME type. Each extractor implements `extract(file_bytes, metadata) -> MediaContent`. New extractors: `VideoExtractor` (ffmpeg keyframes + Whisper transcript), `OfficeExtractor` (python-docx/openpyxl/python-pptx), `AudioExtractor` (Whisper API).

**Rationale**: Current `MediaProcessor` has hardcoded if/else branches. A registry pattern makes adding new types trivial and testable in isolation.

**Alternatives considered**:
- *External document processing service (e.g., Unstructured.io)*: Rejected for now — adds external dependency and cost; revisit if extraction quality is insufficient.
- *Apache Tika*: Rejected — JVM dependency, heavy for our Python stack.

**Implementation**:
- Video: `ffmpeg` extracts 1 keyframe per 30s + audio track; Whisper API transcribes audio. Output: combined transcript + keyframe descriptions (via Gemini vision).
- Office: `python-docx` for .docx, `openpyxl` for .xlsx (cell text + sheet names), `python-pptx` for .pptx (slide text + speaker notes). Output: concatenated text content.
- Audio: Whisper API transcription. Output: transcript text.
- All outputs feed into the existing pipeline as enriched message text.

### D4: Activate Weaviate near-vector search

**Choice**: Add `semantic_search(query_vector, filters, limit)` method to `WeaviateStore`. Uses Weaviate's `near_vector` query with optional metadata filters (channel_id, importance, topic_tags, date range).

**Rationale**: Vectors are already stored. Activation is a store-layer change only — no schema migration needed.

**Alternatives considered**:
- *Hybrid search (BM25 + vector)*: Deferred — requires Weaviate text2vec module config change. Can be added later.
- *External vector DB (Pinecone, Qdrant)*: Rejected — vectors already in Weaviate, no reason to duplicate.

### D5: Fact supersession via contradiction detection

**Choice**: Add a post-classification step that queries Weaviate for existing facts with overlapping entity_tags and topic_tags. If the LLM identifies a contradiction (e.g., "we use Redis" vs "we deprecated Redis"), the new fact gets a `supersedes` field pointing to the old fact's ID, and the old fact's `invalid_at` is set.

**Rationale**: Enterprise knowledge bases must reflect current state. Stale facts erode trust.

**Alternatives considered**:
- *Manual curation UI*: Complementary but doesn't solve automated ingestion.
- *Time-based expiry (TTL)*: Rejected — facts don't expire uniformly; a 2-year-old architecture decision may still be valid.
- *Version chains*: Rejected as over-engineering for v1 — simple supersession pointer is sufficient.

### D6: Cross-batch thread context via persisted parent summaries

**Choice**: When the preprocessor encounters a thread reply whose parent is not in the current batch, query MongoDB/Weaviate for the parent message by `thread_ts`. Store a `thread_parent_summary` field on preprocessed messages.

**Rationale**: Current design only resolves parent context within the same batch. Enterprise Slack threads often span hours/days across multiple ingestion runs.

### D7: Soft orphan handling with grace period

**Choice**: Instead of deleting entities with zero relationships, tag them as `status: "pending"` with a `pending_since` timestamp. A background reconciler promotes entities to `active` if relationships appear within N batches (configurable, default 5), or prunes them after the window expires.

**Rationale**: First mentions of projects/initiatives often lack relationships in their initial batch. Hard deletion loses important entities.

## Risks / Trade-offs

- **[Increased LLM cost]** Coreference resolution adds one LLM call per batch. -> Mitigation: Use smaller model (Gemini Flash) for coreference; skip batches with no pronouns detected (regex pre-filter).
- **[Embedding computation for entity names]** Extra Jina API calls for entity name embeddings. -> Mitigation: Cache embeddings on Neo4j nodes; only compute for new/unseen names.
- **[Video processing latency]** ffmpeg + Whisper can take minutes per video. -> Mitigation: Process media asynchronously; don't block the main pipeline. Use a media processing queue with configurable concurrency.
- **[False entity merges]** Embedding similarity may suggest merging distinct entities with similar names. -> Mitigation: LLM confirmation step; configurable similarity threshold; merge audit log.
- **[Contradiction detection false positives]** LLM may incorrectly flag non-contradictory facts as contradictions. -> Mitigation: Only supersede when confidence > 0.8; keep superseded facts queryable (soft invalidation, not deletion).
- **[Migration complexity]** Adding `name_vector` to existing Entity nodes and `invalid_at`/`supersedes` to existing facts. -> Mitigation: Both are additive fields with null defaults; no destructive migration. Backfill can run asynchronously.
- **[Whisper API cost for audio/video]** -> Mitigation: Configurable per-workspace; can disable audio transcription for cost-sensitive deployments.
