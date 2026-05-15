## ADDED Requirements

### Requirement: Channel workspace route
The frontend SHALL provide a route at `/channels/:id` that renders the channel workspace layout. The layout SHALL include a channel header (channel name and platform badge) and a tab bar.

#### Scenario: Navigate to a channel workspace
- **WHEN** a user navigates to `/channels/C123`
- **THEN** the channel workspace renders with the channel name in the header and the tab bar visible

### Requirement: Tab bar with five tabs
The channel workspace SHALL display a tab bar with tabs: Wiki, Ask, Memories, Graph, Settings. The default active tab SHALL be Wiki. Clicking a tab SHALL switch the content area to that tab's component.

#### Scenario: Default tab is Wiki
- **WHEN** a user navigates to `/channels/C123` without a tab parameter
- **THEN** the Wiki tab is active and its content is displayed

#### Scenario: Switch to Ask tab
- **WHEN** a user clicks the "Ask" tab
- **THEN** the Ask tab becomes active and the Ask component is displayed

### Requirement: Ask tab with streaming input
The Ask tab SHALL provide a text input for questions and a response area. When a user submits a question, the tab SHALL call `POST /api/channels/:id/ask` and consume the SSE stream, rendering response tokens incrementally as they arrive.

#### Scenario: Submit a question and see streaming response
- **WHEN** a user types "what is our tech stack?" and submits
- **THEN** the Ask tab shows a loading indicator, then progressively renders the response text as `response_delta` events arrive, and displays citations and metadata after the `done` event

#### Scenario: Display thinking steps
- **WHEN** the SSE stream includes `thinking` events
- **THEN** the Ask tab renders thinking steps in a collapsible section above the response

#### Scenario: Display error from stream
- **WHEN** the SSE stream includes an `error` event
- **THEN** the Ask tab displays an error message to the user

### Requirement: Wiki tab placeholder
The Wiki tab SHALL display a placeholder message indicating that wiki content will be available after channel sync (M3). This is a non-functional placeholder for M2.

#### Scenario: View Wiki tab
- **WHEN** a user clicks the Wiki tab
- **THEN** a placeholder message is displayed: "Wiki will be available after channel sync."

### Requirement: Channel list sidebar
The frontend SHALL provide a sidebar listing available channels. Each channel entry SHALL show the channel name and platform icon. Clicking a channel SHALL navigate to `/channels/:id`.

#### Scenario: View channel list
- **WHEN** the frontend loads
- **THEN** the sidebar displays a list of channels fetched from `GET /api/channels`

#### Scenario: Click a channel to navigate
- **WHEN** a user clicks "general" in the channel list
- **THEN** the browser navigates to `/channels/C123` and the workspace loads

### Requirement: SSE streaming hook
The frontend SHALL provide a custom React hook `useAsk(channelId)` that manages the SSE connection to `/api/channels/:id/ask`. The hook SHALL return: `ask(question)` function, `response` (accumulated text), `thinking` (array of thinking steps), `citations` (array), `metadata` (object), `isStreaming` (boolean), and `error` (string | null).

#### Scenario: Hook manages SSE lifecycle
- **WHEN** `ask("what is X?")` is called
- **THEN** the hook opens an SSE connection, accumulates `response_delta` events into `response`, sets `isStreaming` to true during streaming and false after `done`, and populates `citations` and `metadata` from their respective events
