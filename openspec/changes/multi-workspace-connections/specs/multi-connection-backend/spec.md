## ADDED Requirements

### Requirement: Multiple connections per platform
The system SHALL allow creating multiple `PlatformConnection` records with the same `platform` value. The database SHALL NOT enforce a unique constraint on the `platform` field.

#### Scenario: Create second Slack connection
- **WHEN** a user creates a connection with `platform: "slack"` and a Slack connection already exists
- **THEN** the system SHALL create the new connection and return `201 Created`

#### Scenario: Database index migration
- **WHEN** the application starts and a unique index on `platform` exists
- **THEN** the system SHALL drop the unique index and create a non-unique compound index on `(platform, source)`

### Requirement: Connection ID in adapter registration
The `_register_adapter()` helper SHALL pass `connection_id` to the bot bridge when registering adapters.

#### Scenario: Register adapter with connection ID
- **WHEN** a new connection is created via `POST /api/connections`
- **THEN** the system SHALL call `POST /bridge/adapters` with `{ platform, credentials, connectionId }` where `connectionId` is the connection's `id` field

#### Scenario: Validate connection passes connection ID
- **WHEN** `POST /api/connections/{id}/validate` is called
- **THEN** `_register_adapter()` SHALL be called with the connection's `id` as `connection_id`

### Requirement: Connection ID in adapter unregistration
The `_unregister_adapter()` helper SHALL use connection ID to target the specific adapter.

#### Scenario: Unregister adapter by connection ID
- **WHEN** a connection is deleted via `DELETE /api/connections/{id}`
- **THEN** the system SHALL call `DELETE /bridge/adapters/{connectionId}` using the connection's `id`

#### Scenario: Rollback on failed creation
- **WHEN** a connection creation fails after adapter registration succeeds
- **THEN** `_unregister_adapter()` SHALL use the connection's `id` (generated before registration) to unregister the correct adapter

### Requirement: Connection-scoped channel listing
The `_list_bridge_channels()` helper SHALL support connection-scoped requests.

#### Scenario: List channels for a specific connection
- **WHEN** `list_connection_channels()` is called with a connection ID
- **THEN** it SHALL call `GET /bridge/connections/{connectionId}/channels` instead of the platform-aggregated route

#### Scenario: Validate connection uses connection-scoped channels
- **WHEN** `validate_connection()` lists channels to verify access
- **THEN** it SHALL use the connection-scoped channel route with the connection's ID

### Requirement: Connection ID in credentials endpoint
The internal credentials endpoint SHALL include connection ID in each entry.

#### Scenario: Fetch credentials for startup sync
- **WHEN** the bot calls `GET /api/internal/connections/credentials`
- **THEN** each entry in the response array SHALL include `connection_id`, `platform`, `credentials`, and `status`

### Requirement: display_name required for UI connections
The system SHALL require a non-empty `display_name` for connections created via the UI (`source: "ui"`).

#### Scenario: Create connection without display_name
- **WHEN** a user creates a connection via `POST /api/connections` with empty or missing `display_name`
- **THEN** the system SHALL return `422 Unprocessable Entity` with an error message

#### Scenario: Env-sourced connections auto-name
- **WHEN** the system creates an env-sourced connection during startup migration
- **THEN** the connection SHALL have `display_name` set to `"{Platform} (env)"` (e.g., `"Slack (env)"`)

### Requirement: Platform literal includes all supported platforms
The `PlatformConnection` model's `platform` field SHALL accept `"slack" | "discord" | "teams" | "telegram"`.

#### Scenario: Create Teams connection
- **WHEN** a user creates a connection with `platform: "teams"`
- **THEN** the system SHALL accept and persist the connection

### Requirement: Env migration scoped to platform
The env migration logic SHALL only skip creation if an env-sourced connection for the same platform already exists.

#### Scenario: Env Slack migration with existing UI Slack
- **WHEN** `SLACK_BOT_TOKEN` is set in env and a UI-sourced Slack connection exists but no env-sourced Slack connection exists
- **THEN** the system SHALL create the env-sourced Slack connection

### Requirement: ChatBridgeAdapter connection-awareness
The `ChatBridgeAdapter` SHALL accept an optional `connection_id` parameter and route requests through connection-scoped bridge endpoints when set.

#### Scenario: Fetch history for a specific connection
- **WHEN** `fetch_history()` is called on an adapter with `connection_id` set
- **THEN** it SHALL request `GET /bridge/connections/{connectionId}/channels/{channelId}/messages`

#### Scenario: List channels for a specific connection
- **WHEN** `list_channels()` is called on an adapter with `connection_id` set
- **THEN** it SHALL request `GET /bridge/connections/{connectionId}/channels`

#### Scenario: Backward-compatible mode
- **WHEN** `ChatBridgeAdapter` is created without `connection_id`
- **THEN** it SHALL use legacy routes (`/bridge/channels/...`) as today

### Requirement: Generate connection ID before registration
The connection creation flow SHALL generate the connection ID before calling `_register_adapter()` so that the same ID is used for both registration and persistence.

#### Scenario: Connection ID consistency
- **WHEN** `POST /api/connections` is called
- **THEN** the connection ID SHALL be generated first, passed to `_register_adapter()`, and then used to persist the connection document
