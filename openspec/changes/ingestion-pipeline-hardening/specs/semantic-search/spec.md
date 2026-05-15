## ADDED Requirements

### Requirement: Near-vector semantic search on facts
The system SHALL support querying Weaviate facts by vector similarity using the stored `text_vector` embeddings.

#### Scenario: Semantic query returns relevant facts
- **WHEN** a query "what database did we choose" is embedded and searched with near_vector
- **THEN** the system SHALL return facts semantically related to database decisions, ranked by vector similarity score

#### Scenario: Semantic search with metadata filters
- **WHEN** a near-vector query is combined with filters (channel_id, importance >= "high", date range)
- **THEN** the system SHALL apply both vector similarity ranking and metadata filtering, returning only facts matching all criteria

#### Scenario: Empty results
- **WHEN** a semantic query has no facts above the minimum similarity threshold (configurable, default 0.7)
- **THEN** the system SHALL return an empty result set rather than low-relevance matches

### Requirement: Hybrid retrieval combining vector and field-based search
The system SHALL support a hybrid retrieval mode that combines semantic vector results with exact field-filter results and deduplicates them.

#### Scenario: Hybrid search merges results
- **WHEN** a query matches 3 facts via vector similarity and 2 facts via exact entity_tag filter, with 1 overlapping fact
- **THEN** the system SHALL return 4 unique facts, with the overlapping fact ranked highest

### Requirement: Search result includes similarity score
The system SHALL include a similarity score (0.0-1.0) with each result from semantic search.

#### Scenario: Scores returned with results
- **WHEN** a semantic search returns 5 facts
- **THEN** each fact SHALL include a `similarity_score` field indicating its cosine similarity to the query vector
