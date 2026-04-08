import { useEffect, useState } from "react";
import {
  BrowserRouter,
  Routes,
  Route,
  useLocation,
  useNavigationType,
} from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { Sidebar } from "@/components/layout/Sidebar";
import { Header } from "@/components/layout/Header";
import { Dashboard } from "@/pages/Dashboard";
import { Channels } from "@/pages/Channels";
import { ChannelWorkspace } from "@/pages/ChannelWorkspace";
import { SearchPage } from "@/pages/SearchPage";
import { GraphExplorer } from "@/pages/GraphExplorer";
import { SettingsPage } from "@/pages/SettingsPage";
import { ActivityPage } from "@/pages/ActivityPage";
import { ProfilePage } from "@/pages/ProfilePage";
import { NotFound } from "@/pages/NotFound";
import { TierBrowser } from "@/components/memories/TierBrowser";
import { AskTab } from "@/components/channel/AskTab";
import { WikiTab } from "@/components/channel/WikiTab";
import { MessagesTab } from "@/components/channel/MessagesTab";
import { GraphTab } from "@/components/graph/GraphTab";
import { ChannelSettingsTab } from "@/components/channel/ChannelSettingsTab";
import { SyncHistoryTab } from "@/components/channel/SyncHistoryTab";
import { useTheme } from "@/hooks/useTheme";
import { ChannelDefaultRedirect } from "@/pages/ChannelWorkspace";

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
    <div className="grid grid-cols-[auto_1fr] h-dvh min-h-screen bg-background">
      <Sidebar open={sidebarOpen} onClose={() => setSidebarOpen(false)} />
      {/* Mobile overlay */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-20 bg-black/30 lg:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}
      {/* Right column: header row + main row */}
      <div className="grid grid-rows-[auto_1fr] min-w-0 overflow-hidden">
        <Header onMenuClick={() => setSidebarOpen(true)} />
        <main className="relative min-h-0 overflow-hidden bg-muted/30">
          <div
            className={`h-full overflow-auto transition-[opacity,transform,filter] duration-280 ease-[cubic-bezier(0.22,1,0.36,1)] ${
              shouldAnimateRoute && !routeVisible
                ? "opacity-0 translate-y-1.5 blur-[0.5px]"
                : "opacity-100 translate-y-0 blur-0"
            }`}
          >
            <Routes location={location}>
              <Route path="/" element={<Dashboard />} />
              <Route path="/channels" element={<Channels />} />
              <Route path="/channels/:id" element={<ChannelWorkspace />}>
                <Route index element={<ChannelDefaultRedirect />} />
                <Route path="wiki" element={<WikiTab />} />
                <Route path="ask" element={<AskTab />} />
                <Route path="messages" element={<MessagesTab />} />
                <Route path="memories" element={<TierBrowser />} />
                <Route path="graph" element={<GraphTab />} />
                <Route path="sync-history" element={<SyncHistoryTab />} />
                <Route path="settings" element={<ChannelSettingsTab />} />
              </Route>
              <Route path="/search" element={<SearchPage />} />
              <Route path="/graph" element={<GraphExplorer />} />
              <Route path="/activity" element={<ActivityPage />} />
              <Route path="/settings" element={<SettingsPage />} />
              <Route path="/profile" element={<ProfilePage />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </div>
        </main>
      </div>
    </div>
  );
}

function App() {
  return (
    <TooltipProvider>
      <BrowserRouter>
        <AppShell />
      </BrowserRouter>
    </TooltipProvider>
  );
}

export default App;
