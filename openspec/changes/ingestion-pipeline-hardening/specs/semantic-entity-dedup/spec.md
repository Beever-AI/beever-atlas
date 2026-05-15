## ADDED Requirements

### Requirement: Embedding-based entity similarity matching
The system SHALL compute embedding vectors for entity names and compare them against known entity name embeddings using cosine similarity to identify semantically equivalent entities that string similarity misses.

#### Scenario: Semantic equivalence detected
- **WHEN** the extracted entity name is "Beever Atlas" and the known entity "Atlas" exists with a cosine similarity of 0.92
- **THEN** the system SHALL flag "Beever Atlas" and "Atlas" as merge candidates

#### Scenario: Similar but distinct entities not merged
- **WHEN** "Redis" and "Redshift" have a cosine similarity of 0.78 (below the 0.85 threshold)
- **THEN** the system SHALL NOT flag them as merge candidates

### Requirement: LLM confirmation before merge
The system SHALL require LLM confirmation before merging embedding-similarity candidates to prevent false merges.

#### Scenario: LLM confirms merge
- **WHEN** embedding similarity flags "Atlas" and "Beever Atlas" as candidates and the LLM confirms they refer to the same entity
- **THEN** the cross-batch validator SHALL merge them under the most complete canonical name ("Beever Atlas") with "Atlas" as an alias

#### Scenario: LLM rejects merge
- **WHEN** embedding similarity flags "Atlas" and "Atlas Corp" as candidates but the LLM determines they are distinct entities (product vs. company)
- **THEN** the system SHALL keep them as separate entities and record the rejection to avoid re-evaluating in future batches

### Requirement: Cache entity name embeddings
The system SHALL cache entity name embeddings on Neo4j Entity nodes in a `name_vector` property to avoid recomputing embeddings for known entities.

#### Scenario: New entity gets embedding computed and cached
- **WHEN** a new entity "Kubernetes" is persisted to Neo4j
- **THEN** the system SHALL compute and store its name embedding in the `name_vector` property

#### Scenario: Known entity uses cached embedding
- **WHEN** computing similarity for a known entity that already has a `name_vector`
- **THEN** the system SHALL use the cached embedding without calling the embedding API

### Requirement: Configurable similarity threshold
The system SHALL use a configurable cosine similarity threshold (default 0.85) for entity merge candidate detection.

#### Scenario: Threshold adjustment
- **WHEN** the threshold is set to 0.90
- **THEN** only entity pairs with cosine similarity >= 0.90 SHALL be flagged as candidates
