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
import { ChannelWorkspace } from "@/pages/ChannelWorkspace";
import { SearchPage } from "@/pages/SearchPage";
import { GraphExplorer } from "@/pages/GraphExplorer";
import { SettingsPage } from "@/pages/SettingsPage";
import { NotFound } from "@/pages/NotFound";
import { TierBrowser } from "@/components/memories/TierBrowser";
import { AskTab } from "@/components/channel/AskTab";
import { WikiTab } from "@/components/channel/WikiTab";
import { MessagesTab } from "@/components/channel/MessagesTab";
import { useTheme } from "@/hooks/useTheme";

function PlaceholderTab({ label }: { label: string }) {
  return (
    <div className="p-6 animate-fade-in">
      <p className="text-muted-foreground text-base">{label} — coming soon.</p>
    </div>
  );
}

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
    <div className="grid grid-cols-[auto_1fr] h-screen bg-background">
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
                <Route index element={<Navigate to="wiki" replace />} />
                <Route path="wiki" element={<WikiTab />} />
                <Route path="ask" element={<AskTab />} />
                <Route path="messages" element={<MessagesTab />} />
                <Route path="memories" element={<TierBrowser />} />
                <Route path="graph" element={<PlaceholderTab label="Graph" />} />
                <Route path="settings" element={<PlaceholderTab label="Channel Settings" />} />
              </Route>
              <Route path="/search" element={<SearchPage />} />
              <Route path="/graph" element={<GraphExplorer />} />
              <Route path="/settings" element={<SettingsPage />} />
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
