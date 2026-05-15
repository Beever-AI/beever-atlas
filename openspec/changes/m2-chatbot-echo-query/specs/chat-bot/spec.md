## ADDED Requirements

### Requirement: Chat SDK initialization with Slack adapter
The bot service SHALL initialize a `Chat` instance with `@chat-adapter/slack` and `@chat-adapter/state-redis`. The bot SHALL read `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, and `REDIS_URL` from environment variables. The Chat instance SHALL be configured with `userName: "beever"`.

#### Scenario: Bot starts successfully with valid credentials
- **WHEN** the bot service starts with valid `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, and `REDIS_URL`
- **THEN** the Chat SDK initializes without error and the bot is ready to receive webhooks

#### Scenario: Bot fails gracefully with missing credentials
- **WHEN** the bot service starts without `SLACK_BOT_TOKEN` or `SLACK_SIGNING_SECRET`
- **THEN** the bot SHALL log an error message and exit with a non-zero code

### Requirement: Webhook route for Slack events
The bot service SHALL expose a POST route at `/api/slack` that delegates to `bot.webhooks.slack` for Slack event processing. The route SHALL handle webhook verification challenges automatically via the Chat SDK.

#### Scenario: Slack sends a URL verification challenge
- **WHEN** Slack sends a `url_verification` challenge to `POST /api/slack`
- **THEN** the bot responds with the challenge token and HTTP 200

#### Scenario: Slack sends an event payload
- **WHEN** Slack sends an `event_callback` payload to `POST /api/slack`
- **THEN** the Chat SDK parses and routes the event to the appropriate handler

### Requirement: @mention handler forwards query to backend
The bot SHALL register an `onNewMention` handler that extracts the user's question text (stripping the @mention prefix), calls `POST /api/channels/:id/ask` on the Python backend with the question, and posts the streamed response back to the Slack thread. The handler SHALL call `thread.subscribe()` to enable follow-up messages.

#### Scenario: User @mentions Beever with a question
- **WHEN** a user sends "@beever what is our deployment process?" in a Slack channel
- **THEN** the bot extracts "what is our deployment process?", calls the backend ask endpoint with the channel ID, and posts the response in the same thread

#### Scenario: Bot subscribes to thread after first mention
- **WHEN** the bot processes an @mention
- **THEN** the bot calls `thread.subscribe()` so subsequent messages in the thread trigger `onSubscribedMessage`

### Requirement: Subscribed message handler for follow-up queries
The bot SHALL register an `onSubscribedMessage` handler that forwards follow-up messages in subscribed threads to the backend ask endpoint, maintaining conversational context within the thread.

#### Scenario: User sends a follow-up in a subscribed thread
- **WHEN** a user sends a follow-up message in a thread where the bot was previously mentioned
- **THEN** the bot forwards the message to the backend ask endpoint and posts the response in the same thread

### Requirement: Response formatting as Slack Block Kit
The bot SHALL format backend responses as Slack Block Kit blocks containing: an answer section (markdown text), a citations section (if citations are present), and a route badge (semantic/graph/hybrid). The bot SHALL use `thread.post()` to send formatted responses.

#### Scenario: Backend returns a response with citations
- **WHEN** the backend streams a response with answer text and citation objects
- **THEN** the bot posts a Slack message with the answer as a markdown section block and citations as a context block

#### Scenario: Backend returns an error
- **WHEN** the backend returns an error event in the SSE stream
- **THEN** the bot posts an error message in the thread indicating the query could not be processed

### Requirement: Multi-adapter architecture
The Chat SDK setup SHALL be structured so that additional adapters (Teams, Discord, Linear) can be added by importing the adapter package and adding it to the `adapters` config object. No changes to handler logic SHALL be required when adding a new platform adapter.

#### Scenario: Adding a new platform adapter
- **WHEN** a developer adds `@chat-adapter/discord` to dependencies and adds `discord: createDiscordAdapter()` to the adapters config
- **THEN** the existing `onNewMention` and `onSubscribedMessage` handlers work for Discord without modification
