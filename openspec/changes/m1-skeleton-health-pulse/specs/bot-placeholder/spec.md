## ADDED Requirements

### Requirement: TypeScript bot service
The system SHALL have a `bot/` directory containing a Node.js TypeScript project with a service that connects to Redis on startup and logs "ready".

#### Scenario: Service starts
- **WHEN** running `npm start` in `bot/`
- **THEN** the service connects to Redis and logs "Bot service ready" to stdout

#### Scenario: Redis connection failure
- **WHEN** Redis is unreachable on startup
- **THEN** the service logs an error and exits with code 1

### Requirement: Docker integration
The system SHALL include a `bot/Dockerfile` that builds the TypeScript project and runs the service. The Dockerfile SHALL be referenced in `docker-compose.yml`.

#### Scenario: Docker build succeeds
- **WHEN** running `docker build` on `bot/Dockerfile`
- **THEN** the image builds successfully

#### Scenario: Service starts in Docker Compose
- **WHEN** running `docker compose up bot`
- **THEN** the bot service starts, connects to the Redis service, and logs "ready"

### Requirement: No platform adapters in M1
The bot service SHALL NOT include any Slack, Teams, or Discord adapter code. It SHALL only prove that the TypeScript service starts and connects to Redis.

#### Scenario: No adapter imports
- **WHEN** inspecting the bot source code
- **THEN** there are no imports from slack-sdk, @microsoft/teams-sdk, or discord.js
