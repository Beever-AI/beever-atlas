import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route, Navigate } from "react-router-dom";

// Mock the heavy tab sub-components so this test exercises *routing* only.
vi.mock("@/components/settings/SyncDefaultsSection", () => ({
  SyncDefaultsSection: () => <div>SYNC_DEFAULTS_TAB</div>,
}));
vi.mock("@/components/settings/EndpointsTab", () => ({
  EndpointsTab: () => <div>ENDPOINTS_TAB</div>,
}));
vi.mock("@/components/settings/EmbeddingTab", () => ({
  EmbeddingTab: () => <div>EMBEDDING_TAB</div>,
}));
vi.mock("@/components/settings/AgentModelsTab", () => ({
  AgentModelsTab: () => <div>AGENT_MODELS_TAB</div>,
}));
vi.mock("@/components/settings/PlatformCard", () => ({
  PlatformCard: ({ connection }: { connection: { id: string } }) => (
    <div>PLATFORM_CARD_{connection.id}</div>
  ),
}));
vi.mock("@/components/settings/ConnectionWizard", () => ({ ConnectionWizard: () => null }));
vi.mock("@/components/settings/FileImportWizard", () => ({ FileImportWizard: () => null }));
vi.mock("@/components/settings/ManageChannelsDialog", () => ({ ManageChannelsDialog: () => null }));

// useConnections fetches on mount; return an empty list so IntegrationsTab
// renders its empty state synchronously after the first effect.
vi.mock("@/hooks/useConnections", () => ({
  useConnections: () => ({ connections: [], loading: false, error: null, refetch: vi.fn() }),
  useDeleteConnection: () => ({ remove: vi.fn(), loading: false, error: null }),
}));

import { SettingsPage, IntegrationsTab } from "../SettingsPage";
import { EndpointsTab } from "@/components/settings/EndpointsTab";
import { EmbeddingTab } from "@/components/settings/EmbeddingTab";
import { AgentModelsTab } from "@/components/settings/AgentModelsTab";
import { SyncDefaultsSection } from "@/components/settings/SyncDefaultsSection";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/settings" element={<SettingsPage />}>
          <Route index element={<Navigate to="integrations" replace />} />
          <Route path="integrations" element={<IntegrationsTab />} />
          <Route path="channels" element={<SyncDefaultsSection />} />
          <Route path="endpoints" element={<EndpointsTab />} />
          <Route path="embedding" element={<EmbeddingTab />} />
          <Route path="agents" element={<AgentModelsTab />} />
          <Route path="ai-setup" element={<Navigate to="/settings/endpoints" replace />} />
          <Route path="*" element={<Navigate to="/settings/integrations" replace />} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  vi.stubGlobal("confirm", vi.fn(() => true));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.clearAllMocks();
});

describe("SettingsPage routing", () => {
  it("redirects /settings to the Integrations tab", async () => {
    renderAt("/settings");
    await waitFor(() => expect(screen.getByText("No connections yet")).toBeTruthy());
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy();
  });

  it("renders the Endpoints tab content at /settings/endpoints", async () => {
    renderAt("/settings/endpoints");
    await waitFor(() => expect(screen.getByText("ENDPOINTS_TAB")).toBeTruthy());
  });

  it("renders the Embedding tab content at /settings/embedding", async () => {
    renderAt("/settings/embedding");
    await waitFor(() => expect(screen.getByText("EMBEDDING_TAB")).toBeTruthy());
  });

  it("renders the Agent models tab content at /settings/agents", async () => {
    renderAt("/settings/agents");
    await waitFor(() => expect(screen.getByText("AGENT_MODELS_TAB")).toBeTruthy());
  });

  it("redirects /settings/ai-setup to /settings/endpoints", async () => {
    renderAt("/settings/ai-setup");
    await waitFor(() => expect(screen.getByText("ENDPOINTS_TAB")).toBeTruthy());
  });

  it("shows the new tab labels in the tab bar", async () => {
    renderAt("/settings/integrations");
    await waitFor(() => expect(screen.getByText("No connections yet")).toBeTruthy());
    expect(screen.getByRole("link", { name: /Endpoints/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Embedding/i })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Agent models/i })).toBeTruthy();
    // The old "AI Setup" tab is gone.
    expect(screen.queryByRole("link", { name: /AI Setup/i })).toBeNull();
  });

  it("falls back to Integrations for an unknown /settings/* sub-path", async () => {
    renderAt("/settings/does-not-exist");
    await waitFor(() => expect(screen.getByText("No connections yet")).toBeTruthy());
  });

  it("navigates between tabs when a tab link is clicked", async () => {
    renderAt("/settings/integrations");
    await waitFor(() => expect(screen.getByText("No connections yet")).toBeTruthy());
    fireEvent.click(screen.getByRole("link", { name: /Agent models/i }));
    await waitFor(() => expect(screen.getByText("AGENT_MODELS_TAB")).toBeTruthy());
  });
});
