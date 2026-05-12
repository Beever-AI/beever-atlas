import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { MemoryRouter, Routes, Route, Navigate } from "react-router-dom";

// Mock the heavy tab sub-components so this test exercises *routing* only.
vi.mock("@/components/settings/SyncDefaultsSection", () => ({
  SyncDefaultsSection: () => <div>SYNC_DEFAULTS_TAB</div>,
}));
vi.mock("@/components/settings/AISetup", () => ({
  AISetup: () => <div>AI_SETUP_TAB</div>,
}));
vi.mock("@/components/settings/EmbeddingSettings", () => ({
  EmbeddingSettings: () => <div>EMBEDDING_LEGACY_TAB</div>,
}));
vi.mock("@/components/settings/AgentModelSettings", () => ({
  AgentModelSettings: () => <div>AGENT_MODELS_LEGACY_TAB</div>,
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

import {
  SettingsPage,
  IntegrationsTab,
  AgentModelsLegacyShell,
} from "../SettingsPage";
import { AISetup } from "@/components/settings/AISetup";
import { EmbeddingSettings } from "@/components/settings/EmbeddingSettings";
import { SyncDefaultsSection } from "@/components/settings/SyncDefaultsSection";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/settings" element={<SettingsPage />}>
          <Route index element={<Navigate to="integrations" replace />} />
          <Route path="integrations" element={<IntegrationsTab />} />
          <Route path="channels" element={<SyncDefaultsSection />} />
          <Route path="ai-setup" element={<AISetup />} />
          <Route path="embedding" element={<EmbeddingSettings />} />
          <Route path="agents" element={<AgentModelsLegacyShell />} />
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
    // The Settings page chrome is present.
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy();
  });

  it("renders the embedding tab content at /settings/embedding", async () => {
    renderAt("/settings/embedding");
    await waitFor(() => expect(screen.getByText("EMBEDDING_LEGACY_TAB")).toBeTruthy());
  });

  it("renders the AI Setup tab content at /settings/ai-setup", async () => {
    renderAt("/settings/ai-setup");
    await waitFor(() => expect(screen.getByText("AI_SETUP_TAB")).toBeTruthy());
  });

  it("falls back to Integrations for an unknown /settings/* sub-path", async () => {
    renderAt("/settings/does-not-exist");
    await waitFor(() => expect(screen.getByText("No connections yet")).toBeTruthy());
  });

  it("navigates between tabs when a tab link is clicked", async () => {
    renderAt("/settings/integrations");
    await waitFor(() => expect(screen.getByText("No connections yet")).toBeTruthy());
    fireEvent.click(screen.getByRole("link", { name: /Agent Models \(legacy\)/i }));
    await waitFor(() => expect(screen.getByText("AGENT_MODELS_LEGACY_TAB")).toBeTruthy());
  });
});
