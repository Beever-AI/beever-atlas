## ADDED Requirements

### Requirement: Dynamic connection list replaces fixed platform grid
The Settings page SHALL display a list of actual connections instead of a fixed grid of platform cards. Each connection SHALL show its platform icon, display name, status, and actions.

#### Scenario: No connections exist
- **WHEN** the user opens Settings and no connections exist
- **THEN** the page SHALL show an empty state with an "Add Connection" call-to-action

#### Scenario: Multiple connections exist
- **WHEN** the user has 2 Slack connections and 1 Discord connection
- **THEN** the page SHALL display 3 connection cards, each showing the platform icon, display name, status badge, and action buttons

#### Scenario: Connection cards show identity
- **WHEN** a connection card is rendered
- **THEN** it SHALL display the platform name, the display_name, the connection status, and the channel count

### Requirement: Add Connection flow with platform picker
The Settings page SHALL provide an "Add Connection" button that opens a platform picker, then launches the ConnectionWizard for the chosen platform.

#### Scenario: Add connection flow
- **WHEN** the user clicks "Add Connection"
- **THEN** a platform picker SHALL appear showing all supported platforms (Slack, Discord, Teams, Telegram)
- **AND** selecting a platform SHALL open the ConnectionWizard for that platform

#### Scenario: Add second connection for same platform
- **WHEN** a Slack connection already exists and the user clicks "Add Connection" and selects Slack
- **THEN** the ConnectionWizard SHALL open for Slack without any restriction

### Requirement: display_name is required in wizard
The ConnectionWizard SHALL require a non-empty display_name before allowing the user to proceed past step 1.

#### Scenario: Empty display name blocks progress
- **WHEN** the user is on step 1 of the wizard and the display name field is empty
- **THEN** the "Next" button SHALL be disabled

#### Scenario: Display name field has helpful placeholder
- **WHEN** the wizard opens for Slack
- **THEN** the display name placeholder SHALL indicate the purpose (e.g., "e.g. Engineering Workspace")

#### Scenario: Display name label is not optional
- **WHEN** the wizard step 1 is rendered
- **THEN** the display name label SHALL show "Display name" without "(optional)"

### Requirement: Connection actions
Each connection card SHALL provide Manage Channels and Disconnect actions.

#### Scenario: Manage channels on a connection
- **WHEN** the user clicks "Manage Channels" on a specific connection
- **THEN** the ManageChannelsDialog SHALL open for that connection's ID

#### Scenario: Disconnect a connection
- **WHEN** the user clicks Disconnect on a connection and confirms
- **THEN** the connection SHALL be deleted and the list SHALL update

### Requirement: Empty state encourages first connection
When no connections exist, the Settings page SHALL show a welcoming empty state that guides users to add their first connection.

#### Scenario: First-time user experience
- **WHEN** the user visits Settings with no connections
- **THEN** the page SHALL display platform icons, a brief explanation of what connecting does, and a prominent "Add Connection" button
