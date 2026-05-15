## 1. Coreference Resolution

- [x] 1.1 Create `CoreferenceResolver` service in `src/beever_atlas/services/coreference_resolver.py` with LLM-based pronoun resolution using conversation window context
- [x] 1.2 Add channel history retrieval (last 20 messages) from MongoDB/Weaviate for cross-batch context window
- [x] 1.3 Add regex-based pronoun pre-filter to skip LLM call when no pronouns/implicit references detected in batch
- [x] 1.4 Integrate `CoreferenceResolver` into preprocessor pipeline — call after text cleaning, before thread context assembly
- [x] 1.5 Preserve original text in `raw_text` field on preprocessed messages alongside resolved `text`
- [x] 1.6 Write coreference resolution prompt (Gemini Flash) with examples for pronoun, demonstrative, and implicit reference resolution
- [x] 1.7 Add unit tests for CoreferenceResolver: pronoun resolution, cross-message references, no-pronoun skip, missing history fallback

## 2. Semantic Entity Deduplication

- [x] 2.1 Add `name_vector` property to Neo4j Entity node schema and create backfill script for existing entities
- [x] 2.2 Extend `EntityRegistry` with `compute_name_embedding(name)` using Jina API and `find_similar(name_vector, threshold)` using cosine similarity
- [ ] 2.3 Update `CrossBatchValidator` to run embedding similarity check before Jaro-Winkler matching, producing merge candidates
- [ ] 2.4 Update cross-batch validator prompt to include embedding-similarity candidates with LLM confirmation/rejection step
- [x] 2.5 Add merge rejection cache (Neo4j or MongoDB) to avoid re-evaluating previously rejected pairs
- [x] 2.6 Add configurable similarity threshold setting (default 0.85) to pipeline configuration
- [x] 2.7 Write tests for semantic dedup: merge confirmation, merge rejection, cached rejection skip, threshold tuning

## 3. Multimodal Expansion

- [x] 3.1 Refactor `MediaProcessor` into registry pattern — create `MediaExtractorRegistry` with `register(mime_type, extractor)` and `extract(file_bytes, metadata)` dispatch
- [x] 3.2 Migrate existing image extractor (Gemini vision) and PDF extractor (pypdf) into registry as `ImageExtractor` and `PdfExtractor`
- [x] 3.3 Create `OfficeExtractor` for .docx (python-docx), .xlsx (openpyxl), .pptx (python-pptx) with text extraction and char limit
- [x] 3.4 Create `VideoExtractor` using ffmpeg for keyframe extraction (1 per 30s) and Whisper API for audio transcription
- [x] 3.5 Create `AudioExtractor` using Whisper API for standalone audio files (.mp3, .wav, .m4a, .ogg)
- [ ] 3.6 Add async media processing queue so video/audio extraction does not block the main pipeline batch
- [x] 3.7 Add configurable size/duration limits for video (default 10min/100MB) and audio (default 30min)
- [x] 3.8 Add dependencies: `python-docx`, `openpyxl`, `python-pptx`, `moviepy` or `ffmpeg-python` to project
- [x] 3.9 Write tests for each extractor: docx, xlsx, pptx, video (mock ffmpeg/whisper), audio, unknown MIME fallback

## 4. Semantic Search Activation

- [x] 4.1 Add `semantic_search(query_vector, filters, limit, threshold)` method to `WeaviateStore` using Weaviate `near_vector` query
- [x] 4.2 Add `hybrid_search(query_vector, filters, limit)` method that merges vector results with field-filter results and deduplicates
- [x] 4.3 Include `similarity_score` field in search results from semantic queries
- [x] 4.4 Add configurable minimum similarity threshold (default 0.7) to filter low-relevance results
- [ ] 4.5 Update API query endpoints to support `search_mode: "semantic" | "exact" | "hybrid"` parameter
- [x] 4.6 Write tests for semantic search: vector query, filtered vector query, hybrid merge, empty results below threshold

## 5. Temporal Fact Lifecycle

- [x] 5.1 Add `superseded_by`, `supersedes`, and `potential_contradiction` fields to AtomicFact schema and Weaviate collection
- [x] 5.2 Create contradiction detection step in pipeline — after classification, query Weaviate for existing facts with overlapping entity/topic tags
- [x] 5.3 Write contradiction detection prompt that compares new fact against candidate existing facts and returns contradiction confidence
- [x] 5.4 Implement fact supersession logic: set `invalid_at` on old fact, `supersedes` on new fact when contradiction confidence >= 0.8
- [x] 5.5 Add `potential_contradiction` flag for low-confidence contradictions (0.5-0.8) without auto-supersession
- [x] 5.6 Update Weaviate query methods to exclude `invalid_at`-set facts by default, with `include_superseded` option
- [x] 5.7 Write tests for contradiction detection: direct contradiction, additive non-contradiction, low-confidence flag, supersession chain

## 6. Cross-Batch Thread Context

- [x] 6.1 Add parent message lookup in preprocessor — query MongoDB by `thread_ts` when parent not in current batch
- [x] 6.2 Add Weaviate fallback lookup for parent message if MongoDB lookup fails
- [x] 6.3 Build `thread_context` string from retrieved parent message (author + truncated text, configurable max 200 chars)
- [x] 6.4 Add configuration toggle for cross-batch thread context (default: enabled) and max parent text length
- [x] 6.5 Write tests for cross-batch thread context: parent found in MongoDB, parent found in Weaviate, parent not found, disabled config

## 7. Soft Orphan Handling

- [x] 7.1 Add `status` ("active" | "pending") and `pending_since` properties to Neo4j Entity node schema
- [ ] 7.2 Update `CrossBatchValidator` orphan removal to set `status: "pending"` instead of deleting
- [x] 7.3 Update `PersisterAgent` to promote pending entities to active when new relationships are created
- [x] 7.4 Create background reconciler task that prunes expired pending entities (configurable: default 5 batches or 7 days)
- [x] 7.5 Update Neo4j graph queries to exclude pending entities by default, with `include_pending` option
- [x] 7.6 Write tests for soft orphan handling: pending creation, promotion on relationship, expiry pruning, query filtering

## 8. Integration & Verification

- [x] 8.1 Run full pipeline end-to-end test with a batch containing: text messages with pronouns, thread replies, images, a .docx attachment, and duplicate entity names
- [x] 8.2 Verify coreference-resolved text produces correct facts (pronouns replaced before extraction)
- [x] 8.3 Verify semantic dedup merges "Atlas" / "Beever Atlas" into one canonical entity
- [x] 8.4 Verify cross-batch thread context resolves parent messages from prior batches
- [x] 8.5 Verify semantic search returns relevant results for natural language queries
- [x] 8.6 Verify fact supersession marks outdated facts as invalid when contradictions are ingested
- [x] 8.7 Verify pending orphan entities survive across batches and get promoted when relationships appear
