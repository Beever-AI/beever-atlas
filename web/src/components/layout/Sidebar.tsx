import { NavLink } from "react-router-dom";
import {
  Home,
  MessageSquare,
  MessageCircleQuestion,
  Activity,
  Settings,
  PanelLeftClose,
  PanelLeft,
  Sun,
  Moon,
  Trash2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { HealthBadge } from "./HealthBadge";
import { ChannelList } from "@/components/channel/ChannelList";
import { SidebarConversationList } from "./SidebarConversationList";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useTheme } from "@/hooks/useTheme";
import { useAskSessions } from "@/contexts/AskSessionsContext";
import { api, adminHeaders } from "@/lib/api";
import { useState, useEffect, useCallback, useRef } from "react";

const MIN_WIDTH = 180;
const MAX_WIDTH = 480;
const DEFAULT_WIDTH = 224;
const WIDTH_KEY = "sidebar:width";

const navItems = [
  { to: "/", icon: Home, label: "Home" },
  { to: "/channels", icon: MessageSquare, label: "Channels" },
  { to: "/ask?new=1", icon: MessageCircleQuestion, label: "Ask" },
  { to: "/activity", icon: Activity, label: "Activity" },
  { to: "/settings", icon: Settings, label: "Settings" },
];

interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

export function Sidebar({ open, onClose }: SidebarProps) {
  const [collapsed, setCollapsed] = useState(false);
  const [width, setWidth] = useState<number>(() => {
    if (typeof window === "undefined") return DEFAULT_WIDTH;
    const stored = Number(window.localStorage.getItem(WIDTH_KEY));
    return stored >= MIN_WIDTH && stored <= MAX_WIDTH ? stored : DEFAULT_WIDTH;
  });
  const [isResizing, setIsResizing] = useState(false);
  const resizingRef = useRef(false);
  const { resolvedTheme, toggleTheme } = useTheme();
  const { isActive: isAskActive } = useAskSessions();

  useEffect(() => {
    window.localStorage.setItem(WIDTH_KEY, String(width));
  }, [width]);

  const onMouseDown = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    resizingRef.current = true;
    setIsResizing(true);
  }, []);

  useEffect(() => {
    const onMove = (e: MouseEvent) => {
      if (!resizingRef.current) return;
      const next = Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, e.clientX));
      setWidth(next);
    };
    const onUp = () => {
      if (!resizingRef.current) return;
      resizingRef.current = false;
      setIsResizing(false);
    };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
    return () => {
      window.removeEventListener("mousemove", onMove);
      window.removeEventListener("mouseup", onUp);
    };
  }, []);

  const themeButton = (
    <button
      onClick={toggleTheme}
      className="p-1 rounded-md hover:bg-muted text-muted-foreground transition-colors shrink-0 flex items-center justify-center"
      aria-label={resolvedTheme === "dark" ? "Switch to light mode" : "Switch to dark mode"}
    >
      {resolvedTheme === "dark" ? <Sun size={14} /> : <Moon size={14} />}
    </button>
  );

  return (
    <aside
      style={collapsed ? undefined : { width }}
      className={cn(
        "relative flex flex-col h-full border-r border-border bg-background shrink-0 overflow-hidden",
        !isResizing && "transition-[width] duration-200 ease-in-out",
        "hidden lg:flex",
        collapsed && "w-14",
        open && "flex fixed inset-y-0 left-0 z-30 lg:relative lg:z-auto"
      )}
    >
      {/* Logo area */}
      <div className={cn(
        "flex items-center h-12 border-b border-border px-3 shrink-0",
        collapsed ? "justify-center" : "justify-between"
      )}>
        {!collapsed && (
          <NavLink to="/" className="flex items-center gap-2 min-w-0 hover:opacity-80 transition-opacity">
            <div className="w-8 h-8 flex items-center justify-center shrink-0">
              <img
                src="/logo-primary.svg"
                alt="Beever Atlas Logo"
                className="w-full h-full object-contain dark:hidden"
              />
              <img
                src="/logo-white.svg"
                alt="Beever Atlas Logo"
                className="w-full h-full object-contain hidden dark:block"
              />
            </div>
            <span className="font-heading text-xl font-medium text-foreground tracking-tight truncate">
              Beever Atlas
            </span>
          </NavLink>
        )}
        <button
          onClick={() => {
            setCollapsed(!collapsed);
            if (open) onClose();
          }}
          className="p-1 rounded-md hover:bg-muted text-muted-foreground transition-colors shrink-0 hidden lg:flex items-center justify-center"
          aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        >
          {collapsed ? <PanelLeft size={16} /> : <PanelLeftClose size={16} />}
        </button>
      </div>

      {/* Nav items */}
      <nav className="py-2 shrink-0">
        {navItems.map(({ to, icon: Icon, label }) => {
          const navLink = (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              onClick={() => { if (open) onClose(); }}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 px-3 py-1.5 text-[14px] transition-all duration-150 rounded-lg relative mx-1",
                  isActive
                    ? "bg-primary/10 text-primary font-medium"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted",
                  collapsed && "justify-center px-0 mx-0"
                )
              }
            >
              <Icon size={16} className="shrink-0" />
              {!collapsed && <span>{label}</span>}
            </NavLink>
          );

          if (collapsed) {
            return (
              <Tooltip key={to}>
                <TooltipTrigger render={navLink} />
                <TooltipContent side="right">{label}</TooltipContent>
              </Tooltip>
            );
          }
          return navLink;
        })}
      </nav>

      {!collapsed && (
        <>
          {isAskActive ? (
            // SidebarConversationList owns its own section headers and
            // keyboard-hint footer; it gets the full bottom pane.
            <div className="flex-1 min-h-0 border-t border-border/50 bg-muted/10 dark:bg-muted/5">
              <SidebarConversationList />
            </div>
          ) : (
            <>
              <div className="px-3 pt-3 pb-1">
                <p className="font-mono text-[9.5px] uppercase tracking-[0.22em] text-muted-foreground/55">
                  Workspaces
                </p>
              </div>
              <ScrollArea className="flex-1 min-h-0 bg-muted/20 dark:bg-muted/10 border-t border-border/50">
                <ChannelList />
              </ScrollArea>
            </>
          )}
        </>
      )}

      {/* Footer: health badge + theme toggle */}
      <div className={cn(
        "p-3 border-t border-border shrink-0",
        collapsed ? "flex flex-col items-center gap-2" : "flex items-center gap-2"
      )}>
        <div className={cn(
          "bg-muted rounded-xl px-2 py-1",
          collapsed ? "" : "flex-1 min-w-0"
        )}>
          <HealthBadge collapsed={collapsed} />
        </div>

        {collapsed ? (
          <>
            <Tooltip>
              <TooltipTrigger render={themeButton} />
              <TooltipContent side="right">
                {resolvedTheme === "dark" ? "Light mode" : "Dark mode"}
              </TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger
                render={
                  <button
                    type="button"
                    onClick={async () => {
                      if (!confirm("Reset all data? This will delete all memories, connections, and settings.")) return;
                      try {
                        // RES-199: backend requires explicit confirmation of the
                        // target Neo4j database + a literal "yes" token. The default
                        // name is "neo4j"; operators who set NEO4J_DATABASE to a
                        // custom value must update this call to match.
                        await api.post(
                          "/api/dev/reset?database=neo4j&i_understand_data_loss=yes",
                          {},
                          { headers: adminHeaders() },
                        );
                        window.location.reload();
                      } catch (e) {
                        alert("Reset failed. Check console.");
                        console.error(e);
                      }
                    }}
                    className="p-1.5 rounded-lg text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 transition-colors"
                  >
                    <Trash2 size={16} />
                  </button>
                }
              />
              <TooltipContent side="right">Reset all data (dev)</TooltipContent>
            </Tooltip>
          </>
        ) : (
          <div className="flex items-center gap-1">
            <Tooltip>
              <TooltipTrigger render={themeButton} />
              <TooltipContent side="top">
                {resolvedTheme === "dark" ? "Light mode" : "Dark mode"}
              </TooltipContent>
            </Tooltip>
            <Tooltip>
              <TooltipTrigger
                render={
                  <button
                    type="button"
                    onClick={async () => {
                      if (!confirm("Reset all data? This will delete all memories, connections, and settings.")) return;
                      try {
                        // RES-199: backend requires explicit confirmation of the
                        // target Neo4j database + a literal "yes" token. The default
                        // name is "neo4j"; operators who set NEO4J_DATABASE to a
                        // custom value must update this call to match.
                        await api.post(
                          "/api/dev/reset?database=neo4j&i_understand_data_loss=yes",
                          {},
                          { headers: adminHeaders() },
                        );
                        window.location.reload();
                      } catch (e) {
                        alert("Reset failed. Check console.");
                        console.error(e);
                      }
                    }}
                    className="p-1.5 rounded-lg text-muted-foreground hover:text-rose-500 hover:bg-rose-500/10 transition-colors"
                  >
                    <Trash2 size={16} />
                  </button>
                }
              />
              <TooltipContent side="top">Reset all data (dev)</TooltipContent>
            </Tooltip>
          </div>
        )}
      </div>
      {!collapsed && (
        <div
          onMouseDown={onMouseDown}
          role="separator"
          aria-orientation="vertical"
          className={cn(
            "absolute top-0 right-0 h-full w-1 cursor-col-resize z-40 hidden lg:block",
            "hover:bg-primary/40 transition-colors",
            isResizing && "bg-primary/60"
          )}
        />
      )}
    </aside>
  );
}
