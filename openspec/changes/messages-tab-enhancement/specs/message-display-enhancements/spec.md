## ADDED Requirements

### Requirement: Date group separators between messages
The Messages tab SHALL insert visual date separators between messages from different calendar days. Separators SHALL display "Today", "Yesterday", or the formatted date (e.g., "Mar 28, 2026").

#### Scenario: Messages spanning multiple days
- **WHEN** loaded messages span 3 different calendar days
- **THEN** 3 date separator headers are rendered between the appropriate message groups

#### Scenario: All messages from today
- **WHEN** all loaded messages are from today
- **THEN** a single "Today" separator is shown above the first message

### Requirement: Full timestamp on hover
Each message SHALL display the full absolute timestamp (e.g., "Apr 3, 2026, 1:45:32 PM") in a tooltip when the user hovers over the relative time display.

#### Scenario: Hover to see full timestamp
- **WHEN** user hovers over "3h ago" on a message
- **THEN** a tooltip shows "Apr 3, 2026, 1:45:32 PM"

### Requirement: Sort order toggle
The Messages tab header SHALL include a toggle to switch between newest-first and oldest-first sort order. The default order SHALL be newest-first. Changing the sort order SHALL re-fetch messages from the API with the new order.

#### Scenario: Toggle to oldest first
- **WHEN** user clicks the sort toggle from "Newest first" to "Oldest first"
- **THEN** messages are re-fetched with `order=asc` and displayed oldest-first

#### Scenario: Default sort order
- **WHEN** user opens the Messages tab for the first time
- **THEN** messages are displayed newest-first

### Requirement: Auto-refresh with new message notification
The Messages tab SHALL poll for new messages every 30 seconds while the tab is active. When new messages are detected, a toast/banner SHALL appear indicating the count of new messages. Clicking the toast SHALL scroll to or reveal the new messages.

#### Scenario: New messages detected
- **WHEN** polling detects 3 new messages since the last fetch
- **THEN** a banner appears: "3 new messages" with a click-to-reveal action

#### Scenario: Tab not active
- **WHEN** user navigates away from the Messages tab
- **THEN** polling SHALL stop to avoid unnecessary API calls

### Requirement: Message activity sparkline
The Messages tab header area SHALL display a small sparkline chart showing message volume per day for the loaded messages. The sparkline SHALL be rendered as inline SVG without external chart dependencies.

#### Scenario: Sparkline rendering
- **WHEN** loaded messages span 7 days with varying daily counts
- **THEN** a sparkline with 7 bars/points is rendered showing relative daily volume

#### Scenario: Single day of messages
- **WHEN** all loaded messages are from one day
- **THEN** sparkline shows a single bar/point

### Requirement: Jump to date
The Messages tab SHALL provide a date picker that, when a date is selected, fetches messages from that date. The fetch SHALL use appropriate API parameters to load messages around the selected date.

#### Scenario: Jump to specific date
- **WHEN** user selects "Mar 15, 2026" from the date picker
- **THEN** messages from March 15, 2026 are fetched and displayed, replacing the current view
