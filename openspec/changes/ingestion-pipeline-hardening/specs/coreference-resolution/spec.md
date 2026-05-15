## ADDED Requirements

### Requirement: Resolve pronoun references to explicit entity names
The system SHALL resolve pronouns and implicit references (e.g., "it", "they", "this", "that", "the project", "the tool") to their explicit antecedent entity names before passing messages to the fact and entity extractors.

#### Scenario: Pronoun resolving to a named entity in the same batch
- **WHEN** a message contains "Alice built Atlas. It uses Redis for caching."
- **THEN** the coreference resolver SHALL rewrite the text to "Alice built Atlas. Atlas uses Redis for caching." before extraction

#### Scenario: Demonstrative reference resolving across messages
- **WHEN** message 1 says "We're evaluating PostgreSQL for the new service" and message 2 says "That looks promising, let's go with it"
- **THEN** the resolver SHALL rewrite message 2 to "PostgreSQL looks promising, let's go with PostgreSQL" (or equivalent explicit form)

#### Scenario: No pronouns or implicit references detected
- **WHEN** a batch of messages contains no pronouns or implicit entity references
- **THEN** the resolver SHALL pass messages through unchanged with no LLM call (cost optimization)

### Requirement: Use conversation window for context
The system SHALL provide the coreference resolver with a sliding window of recent messages: the current batch plus the last N persisted messages from the same channel (configurable, default 20).

#### Scenario: Cross-batch pronoun resolution
- **WHEN** the previous batch contained "Team decided to adopt Kubernetes" and the current batch contains "We started migrating to it yesterday"
- **THEN** the resolver SHALL resolve "it" to "Kubernetes" using the persisted channel history as context

#### Scenario: Channel history unavailable
- **WHEN** channel history cannot be retrieved (first batch or store error)
- **THEN** the resolver SHALL proceed with only the current batch context and log a warning

### Requirement: Preserve original text
The system SHALL preserve the original unmodified message text in a `raw_text` field on the preprocessed message, alongside the coreference-resolved text in the `text` field.

#### Scenario: Original text retained after resolution
- **WHEN** a message "They approved it" is resolved to "The security team approved the Redis migration"
- **THEN** the preprocessed message SHALL have `raw_text: "They approved it"` and `text: "The security team approved the Redis migration"`
