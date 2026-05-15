## ADDED Requirements

### Requirement: Tag relationship-less entities as pending instead of deleting
The system SHALL assign a `status: "pending"` state with a `pending_since` timestamp to extracted entities that have no relationships in the current batch, instead of deleting them.

#### Scenario: Entity with no relationships tagged as pending
- **WHEN** the entity "Project X" is extracted but has no relationships in the current batch or to known entities
- **THEN** the system SHALL persist it to Neo4j with `status: "pending"` and `pending_since: <current_timestamp>`

#### Scenario: Entity with relationships persisted as active
- **WHEN** the entity "PostgreSQL" is extracted with a USES relationship from "Alice"
- **THEN** the system SHALL persist it with `status: "active"` (current behavior)

### Requirement: Promote pending entities when relationships appear
The system SHALL promote a pending entity to `status: "active"` when a subsequent ingestion batch creates a relationship involving that entity.

#### Scenario: Pending entity gains a relationship
- **WHEN** "Project X" was persisted as pending in batch N, and batch N+2 extracts a WORKS_ON relationship from "Bob" to "Project X"
- **THEN** the system SHALL update "Project X" to `status: "active"` and clear `pending_since`

### Requirement: Prune expired pending entities
The system SHALL delete pending entities that have not gained any relationships within a configurable grace window (default: 5 batches or 7 days, whichever comes first).

#### Scenario: Pending entity expires
- **WHEN** "Random Tool" has been pending for 5 batches and 8 days with no relationships created
- **THEN** the background reconciler SHALL delete it from Neo4j

#### Scenario: Pending entity within grace window retained
- **WHEN** "New Initiative" has been pending for 2 batches and 1 day
- **THEN** the system SHALL retain it in Neo4j with its pending status

### Requirement: Pending entities excluded from default graph queries
The system SHALL exclude pending entities from default graph queries but allow explicit inclusion.

#### Scenario: Default query excludes pending
- **WHEN** a graph query requests entities for a channel without specifying include_pending
- **THEN** the system SHALL return only active entities

#### Scenario: Explicit inclusion of pending entities
- **WHEN** a graph query specifies include_pending=true
- **THEN** the system SHALL return both active and pending entities, with pending entities marked accordingly
