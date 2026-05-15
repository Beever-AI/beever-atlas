## ADDED Requirements

### Requirement: `chat` SDK family is pinned to exact versions
`bot/package.json` SHALL declare the `chat` SDK family — `chat`,
`@chat-adapter/slack`, `@chat-adapter/discord`, `@chat-adapter/teams`,
`@chat-adapter/telegram`, `@chat-adapter/state-redis`, and
`chat-adapter-mattermost` — at exact versions (no leading `^` or `~`).

#### Scenario: Caret range on a chat-family package fails CI
- **WHEN** a contributor edits `bot/package.json` to set
  `"chat": "^4.26.0"` (or any non-exact range)
- **THEN** CI fails with a lint step pointing at the non-exact pin

### Requirement: `bot/Dockerfile` verifies npm registry signatures
`bot/Dockerfile` SHALL invoke `npm ci --audit-signatures` when installing
production dependencies. If signature verification fails, the image build
SHALL fail.

#### Scenario: Tampered lockfile entry fails the image build
- **WHEN** an attacker mutates a package tarball signature in the
  registry and a CI build runs
- **THEN** `npm ci --audit-signatures` exits non-zero and the image does
  not build

### Requirement: Dependabot groups updates for the chat SDK family
The `.github/dependabot.yml` config SHALL include a grouped-updates rule
for the chat SDK family so that version bumps across the family land in
a single PR, preventing partial upgrades.

#### Scenario: Upstream chat 4.27.0 release produces one grouped PR
- **WHEN** the chat SDK family publishes a new minor release
- **THEN** Dependabot opens a single PR covering every chat-family
  package, not one PR per package
