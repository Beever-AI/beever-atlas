## ADDED Requirements

### Requirement: Retrieve parent message for cross-batch thread replies
The system SHALL query persisted data (MongoDB or Weaviate) for the parent message when a thread reply's parent is not present in the current ingestion batch.

#### Scenario: Parent message found in persistence
- **WHEN** a thread reply references `thread_ts: "1234567890.000100"` and the parent message exists in MongoDB
- **THEN** the preprocessor SHALL retrieve the parent message text and build `thread_context` as "[Reply to {author}: {text_truncated}]"

#### Scenario: Parent message not found anywhere
- **WHEN** a thread reply's parent message is not in the current batch, MongoDB, or Weaviate
- **THEN** the preprocessor SHALL log a warning and proceed without thread context (same as current behavior)

### Requirement: Thread context enriches extraction quality
The system SHALL pass the resolved thread context to fact and entity extractors so that context-dependent replies produce meaningful facts.

#### Scenario: Context-dependent reply produces valid fact
- **WHEN** parent message says "Should we migrate from MySQL to PostgreSQL?" and the reply says "Yes, let's do it next sprint"
- **THEN** the fact extractor SHALL produce a fact like "Team decided to migrate from MySQL to PostgreSQL next sprint" using the thread context

#### Scenario: Self-contained reply works without parent
- **WHEN** a thread reply says "I deployed the hotfix to production at 3pm"
- **THEN** the fact extractor SHALL produce a valid fact regardless of whether thread context is available

### Requirement: Configurable thread context lookup
The system SHALL support configuring whether cross-batch thread context lookup is enabled (default: enabled) and the maximum parent text length (default: 200 chars).

#### Scenario: Thread context disabled
- **WHEN** cross-batch thread context is disabled in configuration
- **THEN** the preprocessor SHALL only use in-batch parent messages for thread context (current behavior)
