## Why

Enterprise organizations routinely operate multiple workspaces on the same platform — separate Slack workspaces per department, multiple Discord servers for different communities, distinct Teams tenants for subsidiaries. The current one-connection-per-platform constraint blocks enterprise adoption. This needs to ship before any enterprise pilot.

## What Changes

- **BREAKING**: Remove the unique-per-platform constraint at database and API levels, allowing N connections per platform
- **BREAKING**: Redesign the bot's `ChatManager` registry from `Map<string, AdapterEntry>` (keyed by platform) to support composite keys (`platform:connectionId`), enabling multiple adapters of the same platform type
- **BREAKING**: Redesign the Settings page from a fixed 4-card platform grid to a dynamic connection list with an "Add Connection" flow
- Require `display_name` for UI-created connections (needed to distinguish multiple workspaces of the same platform)
- Add connection ID to bot bridge routes and the internal credentials API response
- Extend the `PlatformConnection` model's platform literal to include `"teams" | "telegram"`

## Capabilities

### New Capabilities
- `multi-connection-backend`: Backend support for multiple connections per platform — drop unique index, remove 409 guard, add connection ID to bridge adapter registration/unregistration, update credentials endpoint response
- `multi-connection-bot`: Bot ChatManager and bridge refactor — composite adapter keys, connection-ID-aware routes and handlers, multi-adapter startup sync
- `multi-connection-frontend`: Settings page redesign — dynamic connection list grouped by platform, "Add Connection" button opening platform picker, updated PlatformCard to always show connection identity

### Modified Capabilities

_(none — no existing specs)_

## Impact

- **Database**: Drop `UNIQUE` index on `platform` in `platform_connections` collection; add compound index on `(platform, source)` for env-migration queries
- **Backend API**: `POST /api/connections` removes 409 duplicate check; `DELETE /bridge/adapters/{platform}` becomes `DELETE /bridge/adapters/{connectionId}`; internal credentials endpoint adds `connection_id` field
- **Bot**: `ChatManager` registry key changes from `platform` to `platform:connectionId`; all bridge handler functions gain `connectionId` parameter; Chat SDK adapter map uses composite keys
- **Frontend**: `SettingsPage` completely rewritten; `PlatformCard` updated to always render from a connection (no more "empty platform" state — that moves to the add-connection flow); `ConnectionWizard` requires display_name
- **Env migration**: `_migrate_env_connection()` filters by platform+source instead of just source
