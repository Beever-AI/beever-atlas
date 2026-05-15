## Context

The Messages tab (`MessagesTab.tsx`) fetches up to 100 messages from `GET /api/channels/{id}/messages` and renders them oldest-first in a card layout. The API proxies to the bot bridge (`SlackBridge` or `DiscordBridge`), which calls the platform's message history API. There is no pagination, no filtering, no sort control, and timestamps show only relative time ("3h ago").

The bot bridge already normalizes messages into a `NormalizedMessage` format across platforms. The Discord bridge uses REST API directly; the Slack bridge uses the Slack SDK's `conversations.history`.

## Goals / Non-Goals

**Goals:**
- Users can browse full message history with cursor-based pagination
- Users can sort messages newest-first (default) or oldest-first
- Users can search and filter messages by text, author, date range, and attachments
- Messages display full timestamps and are grouped by date
- Message list auto-refreshes to show new activity
- Activity sparkline gives at-a-glance volume context

**Non-Goals:**
- Server-side full-text search (out of scope — client-side filter on loaded messages only)
- Real-time WebSocket streaming (polling is sufficient for this phase)
- Message editing or deletion from the UI
- Infinite scroll (explicit "Load more" button preferred for predictability)

## Decisions

### 1. Cursor-based pagination using `before` message ID
**Choice**: Use `before=<message_id>` cursor instead of offset-based pagination.
**Why**: Both Discord and Slack APIs natively support `before`/`latest` cursor params. Offset pagination breaks when messages are added/deleted between pages. Cursor pagination is stable and maps 1:1 to platform APIs.
**Alternative rejected**: Offset pagination (`skip=100`) — fragile under concurrent writes, not supported natively by platform APIs.

### 2. Client-side filtering only
**Choice**: Search and filters operate on currently loaded messages in the browser.
**Why**: Avoids building a search index. Messages are already loaded into state. For most channels, loading 200-500 messages covers the useful history. Platform APIs don't support text search on history.
**Alternative rejected**: Server-side Elasticsearch — over-engineered for this feature, adds infrastructure dependency.

### 3. Sort order via API `order` param
**Choice**: Add `order=desc|asc` to the API. Default `desc` (newest first). The bridge fetches accordingly.
**Why**: Slack returns newest-first by default; Discord returns newest-first by default. Matching the default avoids a re-sort. The API param lets the frontend toggle without client-side re-sorting of potentially large lists.
**Alternative rejected**: Client-side sort only — breaks with pagination (you'd need all messages loaded to sort properly).

### 4. Auto-refresh via polling with deduplication
**Choice**: Poll every 30s for messages newer than the latest loaded message ID. Prepend new messages with a toast notification.
**Why**: Simple, reliable, no WebSocket infrastructure needed. The `since` param already exists in the API.
**Alternative rejected**: WebSocket/SSE push — requires new infrastructure, overkill for a monitoring UI.

### 5. SVG sparkline without external chart library
**Choice**: Render the activity sparkline as a simple inline SVG polyline.
**Why**: Avoids adding recharts (~200KB) for a single tiny chart. A 7-day bar/line sparkline is trivial with SVG.
**Alternative rejected**: recharts/visx — heavy dependency for minimal use.

## Risks / Trade-offs

- **[Client-side filter on large message sets]** → If a channel has 10K+ messages loaded via repeated "Load more", filtering may lag. Mitigation: Cap loaded messages at ~1000, add a "showing X of Y" indicator.
- **[Rate limiting on pagination]** → Rapid "Load more" clicks could hit Discord/Slack rate limits. Mitigation: Debounce the button, show loading state, retry with backoff (already implemented for Discord).
- **[Auto-refresh race condition]** → Polling while user is loading more could cause duplicates. Mitigation: Deduplicate by message_id in state, pause polling during pagination loads.
- **[Sparkline data source]** → No pre-aggregated message counts exist. Mitigation: Compute from loaded messages for now; this gives a rough activity shape for the loaded window.
