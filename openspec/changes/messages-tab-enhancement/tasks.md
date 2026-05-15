## 1. API & Bridge — Pagination Support

- [x] 1.1 Add `before` and `order` query params to `GET /api/channels/{channel_id}/messages` in `channels.py`
- [x] 1.2 Update `DiscordBridge.getMessages` in `bridge.ts` to forward `before` param to Discord REST API
- [x] 1.3 Update `SlackBridge.getMessages` in `bridge.ts` to forward `before` as `latest` param to Slack SDK
- [x] 1.4 Update bridge `/bridge/connections/{connId}/channels/{id}/messages` route to parse and forward `before` and `order` query params

## 2. Frontend — Sort & Pagination

- [x] 2.1 Change default message order to newest-first (`order=desc`) in `MessagesTab.tsx`
- [x] 2.2 Add sort toggle button (Newest/Oldest) to the message list header that re-fetches with `order` param
- [x] 2.3 Implement "Load more" button that fetches next page using `before=<oldest_message_id>` cursor
- [x] 2.4 Track pagination state: append loaded pages, disable button when no more messages, show loading state
- [x] 2.5 Update header to show loaded message count (e.g., "150 messages loaded")

## 3. Frontend — Date Separators & Timestamps

- [x] 3.1 Add date group separators between messages from different calendar days ("Today", "Yesterday", "Mar 28, 2026")
- [x] 3.2 Add `title` attribute with full absolute timestamp to the relative time display for hover tooltip
- [x] 3.3 Ensure date separators work correctly in both newest-first and oldest-first sort orders

## 4. Frontend — Search & Filters

- [x] 4.1 Add search input to message list header for client-side text filtering (case-insensitive content match)
- [x] 4.2 Add author filter dropdown populated from unique authors in loaded messages
- [x] 4.3 Add date range filter (from/to date inputs)
- [x] 4.4 Add "Has attachments" toggle filter
- [x] 4.5 Combine all filters with intersection logic — update displayed messages reactively

## 5. Frontend — Auto-Refresh

- [x] 5.1 Implement 30-second polling interval that fetches messages newer than the latest loaded message
- [x] 5.2 Deduplicate incoming messages by `message_id` before prepending to state
- [x] 5.3 Show "N new messages" toast/banner when new messages arrive, with click-to-reveal
- [x] 5.4 Pause polling when Messages tab is not active; resume on tab focus

## 6. Frontend — Jump to Date

- [x] 6.1 Add date picker component in the header area
- [x] 6.2 On date selection, fetch messages from that date using `since` param and replace current view
- [x] 6.3 Reset pagination state and filters when jumping to a new date

## 7. Frontend — Activity Sparkline

- [x] 7.1 Compute daily message counts from loaded messages
- [x] 7.2 Render inline SVG sparkline (bar chart) in the header showing daily volume
- [x] 7.3 Update sparkline when more messages are loaded via pagination

## 8. Testing & Verification

- [x] 8.1 Verify pagination works end-to-end: API → Bridge → Discord/Slack → UI
- [x] 8.2 Verify sort toggle re-fetches and displays messages in correct order
- [x] 8.3 Verify all filters work individually and in combination
- [x] 8.4 Verify auto-refresh detects and displays new messages without duplicates
- [x] 8.5 Verify date separators render correctly across timezone boundaries
