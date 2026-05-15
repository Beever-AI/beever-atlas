## 1. Backend: Database & Model

- [x] 1.1 Update `PlatformConnection` model's `platform` literal to include `"teams" | "telegram"`
- [x] 1.2 Update `platform_store.py` startup: drop unique index on `platform`, create compound index on `(platform, source)`
- [x] 1.3 Add `get_connections_by_platform_and_source()` method for env migration queries

## 2. Backend: API Helper Functions

- [x] 2.1 Update `_register_adapter()` to accept `connection_id` param and pass `connectionId` in the bridge request body
- [x] 2.2 Update `_unregister_adapter()` to accept `connection_id` param and call `DELETE /bridge/adapters/{connectionId}`
- [x] 2.3 Update `_list_bridge_channels()` to accept optional `connection_id` param — when set, call `GET /bridge/connections/{connectionId}/channels`; when unset, use legacy platform route

## 3. Backend: API Endpoints

- [x] 3.1 Remove the 409 duplicate-platform check in `POST /api/connections`
- [x] 3.2 Add validation: require non-empty `display_name` for `source="ui"` connections (return 422 if missing)
- [x] 3.3 Update `create_connection()`: generate connection ID before calling `_register_adapter()`, pass it throughout the flow, use it for rollback
- [x] 3.4 Update `delete_connection()` at line 274: pass `conn.id` to `_unregister_adapter()` instead of `conn.platform`
- [x] 3.5 Update `validate_connection()` at line 293: pass `conn.id` to `_register_adapter()` and `_list_bridge_channels()`
- [x] 3.6 Update `list_connection_channels()` at line 323: pass `conn.id` to `_list_bridge_channels()`
- [x] 3.7 Update `_InternalConnectionItem` model to include `connection_id: str` field; populate it in the credentials endpoint response

## 4. Backend: Env Migration & ChatBridgeAdapter

- [x] 4.1 Update `_migrate_env_connection()` to check by `(platform="slack", source="env")` instead of just `source="env"`
- [x] 4.2 Set `display_name` to `"{Platform} (env)"` for env-sourced connections
- [x] 4.3 Add optional `connection_id` parameter to `ChatBridgeAdapter.__init__()`
- [x] 4.4 When `connection_id` is set, route `fetch_history()` through `/bridge/connections/{connectionId}/channels/{channelId}/messages`
- [x] 4.5 When `connection_id` is set, route `list_channels()` through `/bridge/connections/{connectionId}/channels`
- [x] 4.6 When `connection_id` is set, route `get_channel_info()` through `/bridge/connections/{connectionId}/channels/{channelId}`
- [x] 4.7 When `connection_id` is set, route `fetch_thread()` through connection-scoped thread endpoint

## 5. Bot: ChatManager Refactor

- [x] 5.1 Change adapter registry type to store `{ platform, connectionId, config }` per entry, keyed by composite `{platform}:{connectionId}`
- [x] 5.2 Update `register(platform, credentials, connectionId?)` — use composite key, fall back to `{platform}:{platform}` when no connectionId
- [x] 5.3 Update `unregister(platform, connectionId?)` — remove by composite key
- [x] 5.4 Update `rebuild()` — parse platform from composite key (`key.split(":")[0]`) for adapter factory selection, pass full composite key to Chat SDK adapter map
- [x] 5.5 Update `listAdapters()` to return `{ platform, connectionId, adapter }` for each entry
- [x] 5.6 Add `getAdapterByConnectionId(connectionId)` — find entry where connectionId matches
- [x] 5.7 Add `getAdaptersByPlatform(platform)` — return all entries for a given platform

## 6. Bot: Webhook Routing

- [x] 6.1 Add `POST /api/webhooks/{connectionId}` route — look up adapter by connection ID, call `bot.webhooks[compositeKey](request)`
- [x] 6.2 Update `handleSlackWebhook()` to try all Slack adapters when multiple exist (legacy fallback)
- [x] 6.3 Update `handleGenericWebhook()` to try all adapters for the given platform (legacy fallback)
- [x] 6.4 Log which connection ID handled each webhook for observability

## 7. Bot: Bridge Routes & Handlers

- [x] 7.1 Update `POST /bridge/adapters` handler to extract `connectionId` from body and pass to `register()`
- [x] 7.2 Update `DELETE /bridge/adapters/{connectionId}` — find adapter by connection ID and unregister
- [x] 7.3 Add `GET /bridge/connections/{connectionId}/channels` route and handler
- [x] 7.4 Add `GET /bridge/connections/{connectionId}/channels/{channelId}/messages` route and handler
- [x] 7.5 Add `GET /bridge/connections/{connectionId}/channels/{channelId}` route and handler (channel info)
- [x] 7.6 Add `GET /bridge/connections/{connectionId}/channels/{channelId}/threads/{threadId}/messages` route and handler
- [x] 7.7 Update `getBridge()` to accept optional `connectionId` — when set, look up by connection ID; when unset, fall back to platform lookup
- [x] 7.8 Update legacy `GET /bridge/platforms/{platform}/channels` to aggregate across all connections, adding `connection_id` to each channel object
- [x] 7.9 Update startup sync in `index.ts` to pass `connection_id` from each credentials entry to `register()`

## 8. Frontend: Settings Page Redesign

- [x] 8.1 Rewrite `SettingsPage` to render a dynamic list of connections instead of fixed platform grid
- [x] 8.2 Create "Add Connection" button and platform picker dialog
- [x] 8.3 Create empty state component for when no connections exist (platform icons, explanation, CTA)
- [x] 8.4 Update `PlatformCard` to always render from a connection object (remove null connection state)

## 9. Frontend: ConnectionWizard Updates

- [x] 9.1 Make `display_name` required — remove "(optional)" label, disable "Next" when empty
- [x] 9.2 Update placeholder text to be more descriptive (e.g., "Engineering Workspace")
- [x] 9.3 Fix `display_name: displayName || undefined` to send empty string validation to backend instead of `undefined`

## 10. Testing & Verification

- [x] 10.1 Test creating multiple connections for the same platform via API
- [x] 10.2 Test bot startup sync with multiple connections of the same platform
- [x] 10.3 Test per-connection webhook endpoint routes to correct adapter
- [x] 10.4 Test legacy webhook endpoint tries all adapters for platform
- [x] 10.5 Test connection-scoped bridge routes (channels, messages, threads)
- [x] 10.6 Test Settings page with 0, 1, and 3+ connections
- [x] 10.7 Test disconnect of one connection while others remain active
- [x] 10.8 Test env migration with existing UI connection for same platform
- [x] 10.9 Test ChatBridgeAdapter with connection_id fetches from correct connection
- [x] 10.10 Test validate_connection uses connection-scoped channel listing
