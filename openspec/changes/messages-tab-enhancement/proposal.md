## Why

The Messages tab currently loads a flat list of 100 messages in oldest-first order with no pagination, no filtering, and only relative timestamps. Users cannot browse historical messages, find specific conversations, or understand temporal context. This makes the channel message view unusable for any channel with meaningful volume.

## What Changes

- Default message order reversed to newest-first (most recent messages shown first)
- Full absolute timestamps shown on hover, with date group separators ("Today", "Yesterday", "Mar 28, 2026")
- Cursor-based pagination via `before` parameter, with "Load more" button and total count display
- Sort toggle (newest/oldest first) in the message list header
- Client-side search and filter bar: text search, filter by author, date range, has-attachments
- Jump-to-date picker for navigating to a specific date's messages
- Auto-refresh polling (30-60s) with "New messages" toast notification
- Message volume sparkline chart showing activity per day

## Capabilities

### New Capabilities
- `message-pagination`: Cursor-based pagination with `before` parameter across API, bridge, and UI layers
- `message-filtering`: Client-side search, author filter, date range filter, attachment filter in the Messages tab
- `message-display-enhancements`: Date separators, full timestamps, sort toggle, auto-refresh, and activity sparkline

### Modified Capabilities
<!-- No existing spec-level capabilities are changing requirements -->

## Impact

- **API**: `GET /api/channels/{channel_id}/messages` gains `before` (cursor) and `order` params
- **Bot bridge**: `DiscordBridge.getMessages` and `SlackBridge.getMessages` need `before` param forwarding
- **Frontend**: `MessagesTab.tsx` — major refactor for pagination state, filters, date separators, sparkline
- **Dependencies**: May need a lightweight chart library (e.g., recharts) for the sparkline, or implement with SVG
