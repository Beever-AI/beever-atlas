## ADDED Requirements

### Requirement: NormalizedMessage dataclass
The system SHALL provide a `NormalizedMessage` dataclass in `src/beever_atlas/adapters/base.py` with the following fields: `content` (str), `author` (str), `platform` (str enum: "slack" | "teams" | "discord"), `channel_id` (str), `channel_name` (str), `message_id` (str), `timestamp` (datetime), `thread_id` (str | None), `attachments` (list), `reactions` (list), `reply_count` (int), `raw_metadata` (dict).

#### Scenario: Create a NormalizedMessage from Slack data
- **WHEN** a Slack message payload is normalized
- **THEN** a `NormalizedMessage` is created with `platform="slack"`, `channel_id` from the Slack channel, and `timestamp` parsed from Slack's `ts` field

### Requirement: BaseAdapter abstract class
The system SHALL provide a `BaseAdapter` ABC in `src/beever_atlas/adapters/base.py` with the following abstract methods:
- `async fetch_history(channel_id, since, limit) -> list[NormalizedMessage]`
- `async fetch_thread(channel_id, thread_id) -> list[NormalizedMessage]`
- `async get_channel_info(channel_id) -> ChannelInfo`
- `async list_channels() -> list[ChannelInfo]`

And a concrete method:
- `normalize_message(raw) -> NormalizedMessage` — platform-specific normalization

#### Scenario: Implementing a new platform adapter
- **WHEN** a developer creates a `TeamsAdapter` extending `BaseAdapter`
- **THEN** the developer MUST implement `fetch_history`, `fetch_thread`, `get_channel_info`, `list_channels`, and `normalize_message`

### Requirement: ChannelInfo model
The system SHALL provide a `ChannelInfo` dataclass with fields: `channel_id` (str), `name` (str), `platform` (str), `member_count` (int | None), `topic` (str | None), `purpose` (str | None).

#### Scenario: Retrieve channel info
- **WHEN** `get_channel_info("C123")` is called on a SlackAdapter
- **THEN** a `ChannelInfo` is returned with the channel's name, member count, topic, and purpose from the Slack API

### Requirement: SlackAdapter implementation
The system SHALL provide a `SlackAdapter` in `src/beever_atlas/adapters/slack.py` that extends `BaseAdapter` and uses `slack_sdk.web.async_client.AsyncWebClient` for API calls. The adapter SHALL:
- Read `SLACK_BOT_TOKEN` from environment
- Implement `fetch_history` using `conversations.history` API with pagination
- Implement `fetch_thread` using `conversations.replies` API
- Implement `get_channel_info` using `conversations.info` API
- Implement `list_channels` using `conversations.list` API
- Handle Slack rate limits with exponential backoff

#### Scenario: Fetch channel history with pagination
- **WHEN** `fetch_history("C123", limit=500)` is called and the channel has 500+ messages
- **THEN** the adapter paginates through Slack API responses using `cursor` until the limit is reached or no more messages exist

#### Scenario: Fetch history since a timestamp
- **WHEN** `fetch_history("C123", since=datetime(2024,1,1))` is called
- **THEN** the adapter passes `oldest` parameter to the Slack API to only fetch messages after that timestamp

#### Scenario: Handle Slack rate limiting
- **WHEN** the Slack API returns HTTP 429 with a `Retry-After` header
- **THEN** the adapter waits for the specified duration before retrying the request

#### Scenario: Missing SLACK_BOT_TOKEN
- **WHEN** a `SlackAdapter` is created without `SLACK_BOT_TOKEN` in the environment
- **THEN** the adapter raises a `ConfigurationError` with a descriptive message

### Requirement: MockAdapter with fixture data
The system SHALL provide a `MockAdapter` in `src/beever_atlas/adapters/mock.py` that extends `BaseAdapter` and reads from JSON fixture files instead of calling any platform API. The `MockAdapter` SHALL be activated when `ADAPTER_MOCK=true` is set in the environment. It SHALL be usable as a drop-in replacement for `SlackAdapter` in tests, local development, and CI/CD without requiring platform credentials.

#### Scenario: MockAdapter loads fixture conversations
- **WHEN** `MockAdapter` is initialized with `ADAPTER_MOCK=true`
- **THEN** it loads conversation data from `tests/fixtures/slack_conversations.json` and serves it via `fetch_history`, `fetch_thread`, `get_channel_info`, and `list_channels`

#### Scenario: MockAdapter used in tests without Slack credentials
- **WHEN** tests run in CI/CD without `SLACK_BOT_TOKEN`
- **THEN** the system uses `MockAdapter` and all adapter-dependent tests pass using fixture data

#### Scenario: MockAdapter returns realistic multi-person conversations
- **WHEN** `fetch_history("C_MOCK_GENERAL", limit=100)` is called on `MockAdapter`
- **THEN** it returns `NormalizedMessage` objects from multiple authors with varied timestamps, thread replies, reactions, and realistic content

### Requirement: Conversation fixture data
The system SHALL provide fixture files at `tests/fixtures/` containing realistic multi-person Slack conversations. The fixtures SHALL include:
- At least 6 mock users with distinct roles (e.g., engineer, PM, designer, QA, tech lead, DevOps)
- At least 2 mock channels (`#general`, `#engineering`)
- At least 100 messages spanning multiple days
- Thread conversations (parent + replies) with 3+ participants per thread
- Message patterns: technical discussions, architecture decisions, bug reports, standup updates, casual chat
- Reactions on messages (thumbsup, eyes, white_check_mark)
- Code snippet messages and link-sharing messages
- Messages with varying signal-to-noise ratio (some chatter, some high-value decisions)

#### Scenario: Fixtures contain decision-making conversations
- **WHEN** a developer reads `tests/fixtures/slack_conversations.json`
- **THEN** the data includes at least 3 decision threads where team members discuss options and reach a conclusion (useful for M3-M6 ingestion and retrieval testing)

#### Scenario: Fixtures contain temporal patterns
- **WHEN** fixture messages are sorted by timestamp
- **THEN** they span at least 14 days with realistic distribution (weekday clusters, quiet weekends)

### Requirement: Adapter factory with mock support
The system SHALL provide an `get_adapter(platform)` factory function in `src/beever_atlas/adapters/__init__.py` that returns `MockAdapter` when `ADAPTER_MOCK=true`, otherwise returns the real platform adapter (e.g., `SlackAdapter`). This allows all API endpoints and the bot to seamlessly switch between real and mock data.

#### Scenario: Factory returns MockAdapter in dev mode
- **WHEN** `get_adapter("slack")` is called with `ADAPTER_MOCK=true` in environment
- **THEN** a `MockAdapter` instance is returned

#### Scenario: Factory returns SlackAdapter in production
- **WHEN** `get_adapter("slack")` is called without `ADAPTER_MOCK` in environment
- **THEN** a `SlackAdapter` instance is returned

### Requirement: Adapters package exports
The `src/beever_atlas/adapters/__init__.py` SHALL export `NormalizedMessage`, `BaseAdapter`, `ChannelInfo`, `SlackAdapter`, `MockAdapter`, and `get_adapter`.

#### Scenario: Import adapter classes
- **WHEN** a developer writes `from beever_atlas.adapters import SlackAdapter, MockAdapter, NormalizedMessage, get_adapter`
- **THEN** the imports resolve successfully
