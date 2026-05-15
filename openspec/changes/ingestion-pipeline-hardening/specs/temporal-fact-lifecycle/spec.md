## ADDED Requirements

### Requirement: Detect contradictory facts during ingestion
The system SHALL check newly extracted facts against existing facts with overlapping entity and topic tags to detect contradictions.

#### Scenario: Direct contradiction detected
- **WHEN** existing fact says "Team uses Redis for caching" and a new fact says "Team deprecated Redis and switched to Memcached"
- **THEN** the system SHALL identify this as a contradiction with confidence score

#### Scenario: Non-contradictory update not flagged
- **WHEN** existing fact says "Auth service uses JWT tokens" and new fact says "Auth service added refresh token support"
- **THEN** the system SHALL NOT flag this as a contradiction (additive, not contradictory)

### Requirement: Supersede outdated facts
The system SHALL mark contradicted facts as superseded by linking the new fact to the old fact via a `supersedes` pointer, and setting `invalid_at` on the old fact.

#### Scenario: Fact supersession chain
- **WHEN** fact B supersedes fact A, and later fact C supersedes fact B
- **THEN** fact A SHALL have `invalid_at` set and `superseded_by: B`, fact B SHALL have `invalid_at` set and `superseded_by: C`, fact C SHALL be the current valid fact

#### Scenario: Low-confidence contradiction not auto-superseded
- **WHEN** contradiction detection confidence is below 0.8
- **THEN** the system SHALL NOT automatically supersede the old fact; both facts SHALL coexist with a `potential_contradiction` flag

### Requirement: Superseded facts remain queryable
The system SHALL retain superseded facts in Weaviate (soft invalidation) so they can be queried for historical context.

#### Scenario: Historical query includes superseded facts
- **WHEN** a query explicitly requests historical facts (include_superseded=true)
- **THEN** the system SHALL return both current and superseded facts, with superseded facts marked accordingly

#### Scenario: Default query excludes superseded facts
- **WHEN** a standard query does not specify include_superseded
- **THEN** the system SHALL exclude facts where `invalid_at` is set, returning only current facts
