import { useEffect, useState } from "react";
import {
  BrowserRouter,
  Routes,
  Route,
  Navigate,
  useLocation,
  useNavigationType,
} from "react-router-dom";

import { TooltipProvider } from "@/components/ui/tooltip";
import { Sidebar } from "@/components/layout/Sidebar";
import { Header } from "@/components/layout/Header";
import { Dashboard } from "@/pages/Dashboard";
import { Channels } from "@/pages/Channels";
import { AskPage } from "@/pages/AskPage";
import { SharedAskPage } from "@/pages/SharedAskPage";
import {
  SettingsPage,
  IntegrationsTab,
} from "@/pages/SettingsPage";
import { SyncDefaultsSection } from "@/components/settings/SyncDefaultsSection";
import { EndpointsTab } from "@/components/settings/EndpointsTab";
import { EmbeddingTab } from "@/components/settings/EmbeddingTab";
import { AgentModelsTab } from "@/components/settings/AgentModelsTab";
import { ActivityPage } from "@/pages/ActivityPage";
import { ProfilePage } from "@/pages/ProfilePage";
import { NotFound } from "@/pages/NotFound";
import { PushSources } from "@/pages/admin/PushSources";
import { WikiDrift } from "@/pages/admin/WikiDrift";
import { EntityPages } from "@/pages/admin/EntityPages";
import { AskSessionsProvider } from "@/contexts/AskSessionsContext";
import { TierBrowser } from "@/components/memories/TierBrowser";
import { WikiTab } from "@/components/channel/WikiTab";
import { MessagesTab } from "@/components/channel/MessagesTab";
import { ChannelSettingsTab } from "@/components/channel/ChannelSettingsTab";
import { SyncHistoryTab } from "@/components/channel/SyncHistoryTab";
import { useTheme } from "@/hooks/useTheme";
import { ChannelDefaultRedirect } from "@/pages/ChannelWorkspace";
import { ChannelWorkspaceRoute } from "@/routes/ChannelWorkspaceRoute";
import { SyncStatusProvider } from "@/contexts/SyncStatusContext";

function AppShell() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const location = useLocation();
  const navigationType = useNavigationType();
  const routeKey = `${location.pathname}${location.search}`;
  const shouldAnimateRoute = navigationType !== "POP";
  const [routeVisible, setRouteVisible] = useState(true);

  // Initialize theme on mount — applies .dark class to documentElement
  useTheme();

  useEffect(() => {
    if (!shouldAnimateRoute) {
      setRouteVisible(true);
      return;
    }
    setRouteVisible(false);
    const frame = window.requestAnimationFrame(() => setRouteVisible(true));
    return () => window.cancelAnimationFrame(frame);
  }, [routeKey, shouldAnimateRoute]);

  return (
    <AskSessionsProvider>
    <SyncStatusProvider>
    <div className="grid grid-cols-[auto_1fr] h-screen h-dvh bg-background">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-20 bg-black/30 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      {/* Right column: header row + main row */}
      <div className="flex flex-col min-w-0 overflow-hidden">
        <Header onMenuClick={() => setSidebarOpen(true)} />
        <main className="relative flex-1 min-h-0 overflow-hidden bg-muted/30">
          <div
            className={`h-full overflow-hidden transition-[opacity,transform,filter] duration-280 ease-[cubic-bezier(0.22,1,0.36,1)] ${
              shouldAnimateRoute && !routeVisible
                ? "opacity-0 translate-y-1.5 blur-[0.5px]"
                : "opacity-100 translate-y-0 blur-0"
            }`}
          >
            <Routes location={location}>
              <Route path="/" element={<Dashboard />} />
              <Route path="/channels" element={<Channels />} />
              {/* RES-285 — `ChannelWorkspaceRoute` mounts <ChannelWorkspace
                  key={id} /> so the entire subtree (and every useState in
                  it) resets atomically on channel switch. */}
              <Route path="/channels/:id" element={<ChannelWorkspaceRoute />}>
                <Route index element={<ChannelDefaultRedirect />} />
                <Route path="wiki" element={<WikiTab />} />
                <Route path="wiki/:slug" element={<WikiTab />} />
                {/* Legacy redirects — the wiki + entity graphs now live
                    inside their parent tabs via ?view=graph so existing
                    deep-links and toolbar bookmarks keep working.
                    These remain MORE SPECIFIC than ``wiki/:slug`` so
                    React Router matches them first. */}
                <Route
                  path="wiki/graph"
                  element={<Navigate to="../wiki?view=graph" replace />}
                />
                <Route
                  path="graph"
                  element={<Navigate to="../memories?view=graph" replace />}
                />
                <Route path="messages" element={<MessagesTab />} />
                <Route path="memories" element={<TierBrowser />} />
                <Route path="sync-history" element={<SyncHistoryTab />} />
                <Route path="settings" element={<ChannelSettingsTab />} />
              </Route>
              <Route path="/ask" element={<AskPage />} />
              <Route path="/ask/:sessionId" element={<AskPage />} />
              <Route path="/activity" element={<ActivityPage />} />
              <Route path="/settings" element={<SettingsPage />}>
                <Route index element={<Navigate to="integrations" replace />} />
                <Route path="integrations" element={<IntegrationsTab />} />
                <Route path="channels" element={<SyncDefaultsSection />} />
                <Route path="endpoints" element={<EndpointsTab />} />
                <Route path="embedding" element={<EmbeddingTab />} />
                <Route path="agents" element={<AgentModelsTab />} />
                {/* Legacy redirect — ``/settings/ai-setup`` is the old unified
                    tab; its concerns split into Endpoints + Embedding + Agents. */}
                <Route path="ai-setup" element={<Navigate to="/settings/endpoints" replace />} />
                {/* Unknown ``/settings/*`` sub-path → default tab, not the global 404. */}
                <Route path="*" element={<Navigate to="/settings/integrations" replace />} />
              </Route>
              <Route path="/admin/sources" element={<PushSources />} />
              <Route path="/admin/wiki-drift" element={<WikiDrift />} />
              <Route
                path="/admin/entity-pages/:channelId"
                element={<EntityPages />}
              />
              <Route path="/profile" element={<ProfilePage />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </div>
        </main>
      </div>
    </div>
    </SyncStatusProvider>
    </AskSessionsProvider>
  );
}

function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <Routes>
          {/* Public (unauthed) share route — MUST be outside AppShell so it
              renders without sidebar/header chrome and without any auth guard. */}
          <Route path="/ask/shared/:token" element={<SharedAskPage />} />
          <Route path="*" element={<AppShell />} />
        </Routes>
      </BrowserRouter>
    </TooltipProvider>
  );
}

export default App;
