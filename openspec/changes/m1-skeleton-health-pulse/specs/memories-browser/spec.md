## ADDED Requirements

### Requirement: TierBrowser layout
The system SHALL provide a `TierBrowser.tsx` component at `/channels/:id/memories` that displays a 3-tier accordion/column layout: Tier 0 (channel summary) at top, Tier 1 (topic clusters) as expandable cards, Tier 2 (atomic facts) nested under their parent cluster.

#### Scenario: Three tiers render
- **WHEN** navigating to `/channels/test-channel/memories`
- **THEN** the page shows the channel summary at top, topic clusters below, and atomic facts are accessible by expanding a cluster

### Requirement: SummaryCard component
The system SHALL provide a `SummaryCard.tsx` rendering the Tier 0 channel summary. It SHALL always be visible at the top of the memories view, showing the channel name, summary text, last updated timestamp, and message count.

#### Scenario: Summary card display
- **WHEN** the memories page loads
- **THEN** the SummaryCard is visible at the top with channel name and summary text

### Requirement: ClusterCard component
The system SHALL provide a `ClusterCard.tsx` rendering Tier 1 topic clusters as expandable cards. Each card SHALL show the topic label, fact count, and date range. Expanding a card SHALL reveal the member atomic facts.

#### Scenario: Cluster expansion
- **WHEN** clicking on a ClusterCard
- **THEN** it expands to show all member FactCards for that cluster

#### Scenario: Cluster metadata display
- **WHEN** a ClusterCard renders
- **THEN** it shows the topic label, number of facts, and date range

### Requirement: FactCard component
The system SHALL provide a `FactCard.tsx` rendering Tier 2 atomic facts. Each card SHALL show the fact text, quality score badge (color-coded: green >= 7, amber >= 4, red < 4), timestamp, author attribution, and entity tags.

#### Scenario: Fact card display
- **WHEN** a FactCard renders
- **THEN** it shows fact text, quality badge, timestamp, author, and tags

#### Scenario: Quality score coloring
- **WHEN** a fact has quality score 8.5
- **THEN** the badge is green

#### Scenario: Fact detail expansion
- **WHEN** clicking on a FactCard
- **THEN** an expanded detail view shows full metadata and a link placeholder for the original message

### Requirement: MemoryFilters component
The system SHALL provide a `MemoryFilters.tsx` component with filters for: topic (dropdown), entity (text search), minimum importance (slider), and date range (date pickers).

#### Scenario: Filter by topic
- **WHEN** selecting a topic from the dropdown
- **THEN** only clusters and facts matching that topic are displayed

### Requirement: Mock data for M1
The system SHALL use hardcoded mock data conforming to the TypeScript types (`MemoryTier0`, `MemoryTier1`, `MemoryTier2`) to populate the memories browser. Mock data SHALL include at least 1 summary, 3 clusters, and 5 facts.

#### Scenario: Mock data renders
- **WHEN** the memories page loads with no backend
- **THEN** mock data is displayed showing the 3-tier structure

### Requirement: useMemories hook
The system SHALL provide a `useMemories.ts` hook that manages state for the 3-tier memory data. In M1, it SHALL return mock data. The hook interface SHALL match the future API contract: `useMemories(channelId)` returning `{ summary, clusters, facts, filters, setFilters, isLoading }`.

#### Scenario: Hook returns mock data
- **WHEN** calling `useMemories("test-channel")`
- **THEN** it returns summary, clusters, and facts with isLoading=false
