## ADDED Requirements

### Requirement: Composite adapter keys in ChatManager
The `ChatManager` SHALL use `{platform}:{connectionId}` as the adapter registry key, allowing multiple adapters of the same platform type.

#### Scenario: Register two Slack adapters
- **WHEN** `register("slack", creds1, "conn-1")` and `register("slack", creds2, "conn-2")` are called
- **THEN** the adapter map SHALL contain both entries keyed as `"slack:conn-1"` and `"slack:conn-2"`
- **AND** the rebuilt Chat instance SHALL have both adapters active

#### Scenario: Unregister one of multiple adapters
- **WHEN** `unregister("slack", "conn-1")` is called and `"slack:conn-2"` exists
- **THEN** only `"slack:conn-1"` SHALL be removed
- **AND** the Chat instance SHALL rebuild with `"slack:conn-2"` still active

### Requirement: Rebuild extracts platform from composite key
The `rebuild()` method SHALL parse the platform portion from composite keys (e.g., `"slack"` from `"slack:conn-1"`) to select the correct adapter factory, while passing the full composite key to the Chat SDK adapter map.

#### Scenario: Adapter factory selection with composite keys
- **WHEN** `rebuild()` iterates over adapters with key `"slack:conn-1"`
- **THEN** it SHALL use `"slack"` (extracted from the key) to select the Slack adapter factory
- **AND** pass `"slack:conn-1"` as the key in the Chat SDK `adapters` map

#### Scenario: Mixed platform adapters
- **WHEN** adapters include `"slack:conn-1"`, `"slack:conn-2"`, and `"discord:conn-3"`
- **THEN** rebuild SHALL create two Slack adapters and one Discord adapter, all with their composite keys

### Requirement: Connection ID in register/unregister API
The `register` and `unregister` methods SHALL accept an optional `connectionId` parameter. When omitted, the system SHALL fall back to using platform name as the key (backward compatibility for env-sourced connections).

#### Scenario: Register with connection ID
- **WHEN** the bridge receives `POST /bridge/adapters` with `{ platform: "slack", credentials: {...}, connectionId: "abc-123" }`
- **THEN** `ChatManager.register("slack", credentials, "abc-123")` SHALL be called

#### Scenario: Register without connection ID (legacy)
- **WHEN** the bridge receives `POST /bridge/adapters` with `{ platform: "slack", credentials: {...} }` and no `connectionId`
- **THEN** `ChatManager.register("slack", credentials)` SHALL be called with key `"slack:slack"` (platform as fallback ID)

### Requirement: Per-connection webhook endpoints
The bot SHALL support `POST /api/webhooks/{connectionId}` to route webhook requests directly to the correct adapter.

#### Scenario: Webhook for specific connection
- **WHEN** `POST /api/webhooks/abc-123` is received and adapter `"slack:abc-123"` exists
- **THEN** the bot SHALL call `bot.webhooks["slack:abc-123"](request)` and return the response

#### Scenario: Unknown connection ID
- **WHEN** `POST /api/webhooks/unknown-id` is received and no adapter with that connection ID exists
- **THEN** the bot SHALL return `404 Not Found`

### Requirement: Legacy platform webhook fallback
Legacy webhook endpoints (`POST /api/slack`, etc.) SHALL try all adapters for that platform sequentially.

#### Scenario: Legacy Slack webhook with multiple connections
- **WHEN** `POST /api/slack` is received and two Slack adapters exist (`"slack:conn-1"`, `"slack:conn-2"`)
- **THEN** the bot SHALL try each Slack adapter's `handleWebhook()` until one succeeds (returns non-error status)
- **AND** return that adapter's response

#### Scenario: Legacy webhook with single connection
- **WHEN** `POST /api/slack` is received and one Slack adapter exists
- **THEN** the bot SHALL route to that adapter (no change in behavior)

### Requirement: Connection-scoped bridge routes
The bridge SHALL support routes that target a specific connection by ID.

#### Scenario: Delete adapter by connection ID
- **WHEN** `DELETE /bridge/adapters/{connectionId}` is received
- **THEN** the bridge SHALL find the adapter with matching connection ID and unregister it

#### Scenario: List channels for specific connection
- **WHEN** `GET /bridge/connections/{connectionId}/channels` is received
- **THEN** the bridge SHALL return channels from only that connection's adapter

#### Scenario: Fetch messages for specific connection
- **WHEN** `GET /bridge/connections/{connectionId}/channels/{channelId}/messages` is received
- **THEN** the bridge SHALL fetch messages using only that connection's adapter

#### Scenario: Get channel info for specific connection
- **WHEN** `GET /bridge/connections/{connectionId}/channels/{channelId}` is received
- **THEN** the bridge SHALL return channel info from that connection's adapter

### Requirement: Legacy platform routes aggregate connections
Existing routes that use platform name SHALL aggregate results across all connections for that platform.

#### Scenario: List channels by platform with multiple connections
- **WHEN** `GET /bridge/platforms/slack/channels` is received and two Slack connections exist
- **THEN** the response SHALL include channels from both Slack connections
- **AND** each channel object SHALL include a `connection_id` field identifying which connection it belongs to

### Requirement: Startup sync with connection IDs
The bot startup sync SHALL use connection IDs from the backend credentials endpoint.

#### Scenario: Load multiple connections at startup
- **WHEN** the bot fetches `GET /api/internal/connections/credentials` and receives two Slack entries with different `connection_id` values
- **THEN** `register` SHALL be called once per entry with the respective `connectionId`
- **AND** the Chat instance SHALL have both adapters active

### Requirement: ChatManager adapter lookup by connection ID
The `ChatManager` SHALL support looking up adapters by connection ID.

#### Scenario: Get adapter by connection ID
- **WHEN** `getAdapterByConnectionId("conn-1")` is called and `"slack:conn-1"` is registered
- **THEN** it SHALL return the adapter entry with platform `"slack"` and connectionId `"conn-1"`

#### Scenario: Get all adapters for a platform
- **WHEN** `getAdaptersByPlatform("slack")` is called and `"slack:conn-1"` and `"slack:conn-2"` exist
- **THEN** it SHALL return both adapter entries

### Requirement: ChatManager lists adapters with metadata
The `listAdapters` method SHALL return platform, connection ID, and adapter reference for each registered adapter.

#### Scenario: List adapters
- **WHEN** `listAdapters()` is called with two Slack and one Discord adapter registered
- **THEN** the result SHALL contain three entries, each with `{ platform, connectionId, adapter }`

### Requirement: getBridge connection-awareness
The `getBridge()` function SHALL support lookup by connection ID in addition to platform name.

#### Scenario: Get bridge by connection ID
- **WHEN** `getBridge(chatManager, "slack", "conn-1")` is called
- **THEN** it SHALL return the bridge for the adapter keyed `"slack:conn-1"`

#### Scenario: Get bridge by platform only (legacy)
- **WHEN** `getBridge(chatManager, "slack")` is called without connection ID and one Slack adapter exists
- **THEN** it SHALL return the bridge for that adapter (backward compatible)
