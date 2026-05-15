## ADDED Requirements

### Requirement: Client-side text search across loaded messages
The Messages tab SHALL provide a search input that filters the displayed messages by matching against message content. Filtering SHALL be case-insensitive and update results as the user types.

#### Scenario: Search by keyword
- **WHEN** user types "deployment" in the search box
- **THEN** only messages containing "deployment" (case-insensitive) are displayed

#### Scenario: Clear search
- **WHEN** user clears the search input
- **THEN** all loaded messages are displayed again

### Requirement: Filter messages by author
The Messages tab SHALL provide an author filter dropdown populated with the unique authors from loaded messages. Selecting an author SHALL show only their messages.

#### Scenario: Filter by single author
- **WHEN** user selects "Alice" from the author filter
- **THEN** only messages authored by "Alice" are displayed

#### Scenario: Clear author filter
- **WHEN** user clears the author filter
- **THEN** all loaded messages are displayed

### Requirement: Filter messages by date range
The Messages tab SHALL provide date range inputs (from/to) that filter displayed messages to only those within the selected range.

#### Scenario: Filter by date range
- **WHEN** user sets date range from "2026-03-01" to "2026-03-15"
- **THEN** only messages with timestamps within that range are displayed

### Requirement: Filter messages with attachments
The Messages tab SHALL provide a toggle to show only messages that contain attachments.

#### Scenario: Toggle attachment filter
- **WHEN** user enables "Has attachments" filter
- **THEN** only messages with at least one attachment are displayed

### Requirement: Combined filters work together
All filters (search, author, date range, attachments) SHALL be combinable. The displayed messages SHALL be the intersection of all active filters.

#### Scenario: Multiple filters active
- **WHEN** user searches "bug" AND filters by author "Bob" AND enables "Has attachments"
- **THEN** only messages from Bob containing "bug" that have attachments are displayed
