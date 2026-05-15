## ADDED Requirements

### Requirement: React project initialization
The system SHALL have a `web/` directory containing a Vite + React 19 + TypeScript project with TailwindCSS and shadcn/ui configured.

#### Scenario: Dev server starts
- **WHEN** running `npm run dev` in `web/`
- **THEN** a development server starts on port 5173 serving the React app

#### Scenario: Production build succeeds
- **WHEN** running `npm run build` in `web/`
- **THEN** the build completes without errors producing static files in `web/dist/`

### Requirement: Route structure
The system SHALL configure React Router v7 with route stubs for: `/` (Dashboard), `/channels` (Channel list), `/channels/:id` (Channel workspace), `/search` (Search), `/graph` (Graph Explorer), `/settings` (Settings).

#### Scenario: Navigation to all routes
- **WHEN** navigating to any defined route
- **THEN** the corresponding page component renders without errors

#### Scenario: Unknown route shows 404
- **WHEN** navigating to an undefined route
- **THEN** a "Not Found" page is displayed

### Requirement: Layout shell
The system SHALL render a root layout with `Sidebar.tsx` (nav links with icons, collapse toggle, 240px expanded / 64px collapsed) and `Header.tsx` (page title, breadcrumb placeholder).

#### Scenario: Sidebar navigation
- **WHEN** the app loads
- **THEN** the sidebar shows navigation links for Dashboard, Channels, Search, Graph Explorer, Settings

#### Scenario: Sidebar collapse
- **WHEN** clicking the collapse toggle
- **THEN** the sidebar collapses from 240px to 64px, showing only icons

### Requirement: HealthBadge component
The system SHALL include a `HealthBadge.tsx` component that polls `GET /api/health` every 30 seconds and displays a status indicator: green (healthy), amber (degraded), red (unhealthy), gray (loading/unreachable).

#### Scenario: Healthy status display
- **WHEN** the health endpoint returns status "healthy"
- **THEN** the badge shows a green indicator with "All systems operational"

#### Scenario: Degraded status display
- **WHEN** the health endpoint returns status "degraded"
- **THEN** the badge shows an amber indicator with the names of degraded components

#### Scenario: API unreachable
- **WHEN** the health endpoint is unreachable
- **THEN** the badge shows a gray indicator with "Unable to connect"

### Requirement: API client
The system SHALL provide `lib/api.ts` with a fetch wrapper using `VITE_API_URL` as the base URL, JSON content type defaults, and error handling that throws typed errors.

#### Scenario: Successful API call
- **WHEN** calling `api.get("/api/health")` with the backend running
- **THEN** the response JSON is returned as a typed object

### Requirement: TypeScript type definitions
The system SHALL provide `lib/types.ts` with TypeScript interfaces mirroring backend schemas: `HealthResponse`, `ComponentHealth`, `AskResponse`, `Citation`, `WikiResponse`, `SyncResponse`, `ChannelInfo`, `MemoryTier0`, `MemoryTier1`, `MemoryTier2`.

#### Scenario: Types match backend schemas
- **WHEN** importing types from `lib/types.ts`
- **THEN** all interfaces are available and match the field names defined in `docs/v2/12-api-design.md`

### Requirement: Dashboard home page
The system SHALL render a dashboard at `/` with stat card placeholders (channels synced, total memories, last sync time, system health) and the HealthBadge component.

#### Scenario: Dashboard renders
- **WHEN** navigating to `/`
- **THEN** the dashboard shows placeholder stat cards and the HealthBadge

### Requirement: Design tokens
The system SHALL configure TailwindCSS with design tokens: Inter font family, slate/indigo color palette, 4px base spacing unit, and card component styles (rounded-lg, shadow-sm, border).

#### Scenario: Design tokens applied
- **WHEN** rendering any component
- **THEN** text uses Inter font, primary colors use indigo palette, and cards have consistent rounded/shadow styling
