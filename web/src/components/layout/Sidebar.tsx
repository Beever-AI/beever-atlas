import { NavLink } from "react-router-dom";
import {
  LayoutDashboard,
  MessageSquare,
  Search,
  Network,
  Activity,
  Settings,
  PanelLeftClose,
  PanelLeft,
  Sun,
  Moon,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { HealthBadge } from "./HealthBadge";
import { ChannelList } from "@/components/channel/ChannelList";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Separator } from "@/components/ui/separator";
import { useTheme } from "@/hooks/useTheme";
import { useState } from "react";

const navItems = [
  { to: "/", icon: LayoutDashboard, label: "Dashboard" },
  { to: "/channels", icon: MessageSquare, label: "Channels" },
  { to: "/search", icon: Search, label: "Search" },
  { to: "/graph", icon: Network, label: "Graph Explorer" },
  { to: "/activity", icon: Activity, label: "Activity" },
  { to: "/settings", icon: Settings, label: "Settings" },
];

interface SidebarProps {
  open: boolean;
  onClose: () => void;
}

export function Sidebar({ open, onClose }: SidebarProps) {
  const [collapsed, setCollapsed] = useState(false);
  const { resolvedTheme, toggleTheme } = useTheme();

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
      className={cn(
        "flex flex-col border-r border-border bg-background shrink-0 transition-all duration-200 ease-in-out",
        "hidden lg:flex",
        collapsed ? "w-14" : "w-56",
        open && "flex fixed inset-y-0 left-0 z-30 w-56 lg:relative lg:z-auto"
      )}
    >
      {/* Logo area */}
      <div className={cn(
        "flex items-center h-12 border-b border-border px-3 shrink-0",
        collapsed ? "justify-center" : "justify-between"
      )}>
        {!collapsed && (
          <div className="flex items-center gap-2 min-w-0">
            <div className="w-8 h-8 rounded-[10px] flex items-center justify-center shrink-0 overflow-hidden bg-white shadow-sm ring-1 ring-border/50">
              <img src="/logo.png" alt="Beever Atlas Logo" className="w-full h-full object-cover" />
            </div>
            <span className="font-heading text-xl font-medium text-foreground tracking-tight truncate">
              Beever Atlas
            </span>
          </div>
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
                  "flex items-center gap-2.5 px-3 py-2 text-[15px] transition-all duration-150 rounded-xl relative mx-1",
                  isActive
                    ? "bg-primary/10 text-primary font-medium"
                    : "text-muted-foreground hover:text-foreground hover:bg-muted rounded-xl",
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
          <Separator />
          <ScrollArea className="flex-1 py-2">
            <ChannelList />
          </ScrollArea>
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
          <Tooltip>
            <TooltipTrigger render={themeButton} />
            <TooltipContent side="right">
              {resolvedTheme === "dark" ? "Light mode" : "Dark mode"}
            </TooltipContent>
          </Tooltip>
        ) : (
          themeButton
        )}
      </div>
    </aside>
  );
}
