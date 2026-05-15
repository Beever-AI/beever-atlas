## ADDED Requirements

### Requirement: API supports cursor-based pagination with before parameter
The `GET /api/channels/{channel_id}/messages` endpoint SHALL accept an optional `before` query parameter containing a message ID. When provided, the API SHALL return messages older than the specified message. The endpoint SHALL also accept an `order` parameter (`asc` or `desc`) defaulting to `desc`.

#### Scenario: Fetch first page (no cursor)
- **WHEN** client requests `/api/channels/{id}/messages?limit=50`
- **THEN** API returns the 50 most recent messages in newest-first order

#### Scenario: Fetch next page with before cursor
- **WHEN** client requests `/api/channels/{id}/messages?limit=50&before=msg_abc123`
- **THEN** API returns 50 messages older than `msg_abc123` in newest-first order

#### Scenario: Fetch messages in ascending order
- **WHEN** client requests `/api/channels/{id}/messages?limit=50&order=asc`
- **THEN** API returns the 50 oldest messages in oldest-first order

### Requirement: Bridge forwards before parameter to platform APIs
The bot bridge SHALL forward the `before` parameter to the underlying platform API (Discord REST `?before=`, Slack `conversations.history` `latest` param) when provided.

#### Scenario: Discord bridge pagination
- **WHEN** bridge receives a getMessages request with `before=msg_id`
- **THEN** bridge calls Discord REST API with `?before=msg_id&limit=N`

#### Scenario: Slack bridge pagination
- **WHEN** bridge receives a getMessages request with `before=msg_ts`
- **THEN** bridge calls Slack `conversations.history` with `latest=msg_ts`

### Requirement: UI displays Load More button for pagination
The Messages tab SHALL display a "Load more" button at the bottom of the message list when the current page returned exactly `limit` messages (indicating more may exist). Clicking it SHALL fetch the next page using the oldest loaded message's ID as the `before` cursor.

#### Scenario: Load more messages
- **WHEN** user clicks "Load more" and 50 messages were previously loaded
- **THEN** UI fetches next page with `before=<oldest_message_id>` and appends results to the list

#### Scenario: No more messages available
- **WHEN** a fetch returns fewer messages than the requested limit
- **THEN** the "Load more" button SHALL not be displayed

### Requirement: UI displays message count context
The Messages tab header SHALL display the count of currently loaded messages (e.g., "150 messages loaded").

#### Scenario: Message count display
- **WHEN** user has loaded 150 messages across 3 pages
- **THEN** header shows "150 messages loaded"
