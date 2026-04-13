import { useLocation, NavLink } from "react-router-dom";
import {
  Menu,
  Home,
  MessageSquare,
  MessageCircleQuestion,
  Activity,
  Settings,
  UserRound,
  type LucideIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useUserProfile } from "@/hooks/useUserProfile";
import { cn } from "@/lib/utils";

interface HeaderProps {
  onMenuClick: () => void;
}

interface PageMeta {
  title: string;
  subtitle: string;
  icon: LucideIcon;
}

const PAGE_META: Record<string, PageMeta> = {
  "/": { title: "Home", subtitle: "Your workspace at a glance", icon: Home },
  "/channels": { title: "Channels", subtitle: "Browse conversations & memory", icon: MessageSquare },
  "/ask": { title: "Ask", subtitle: "Talk to your knowledge base", icon: MessageCircleQuestion },
  "/settings": { title: "Settings", subtitle: "Preferences & integrations", icon: Settings },
  "/activity": { title: "Activity", subtitle: "Recent events across channels", icon: Activity },
  "/profile": { title: "My Profile", subtitle: "Your identity in Beever Atlas", icon: UserRound },
};

export function Header({ onMenuClick }: HeaderProps) {
  const location = useLocation();
  const isChannel = location.pathname.startsWith("/channels/");
  const meta = PAGE_META[location.pathname];
  const Icon = meta?.icon;
  const { profile } = useUserProfile();

  const emoji = profile.avatarEmoji || "🦫";
  const avatarTitle = profile.displayName
    ? `${profile.displayName}${profile.jobTitle ? ` — ${profile.jobTitle}` : ""}`
    : "My Profile";

  if (isChannel) {
    return (
      <header className="flex items-center h-10 px-3 border-b border-border bg-background shrink-0 gap-2.5 lg:hidden">
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          onClick={onMenuClick}
          aria-label="Open navigation"
        >
          <Menu size={16} />
        </Button>
        <Separator orientation="vertical" className="h-4" />
        <h1 className="text-sm font-semibold text-foreground">Channel</h1>
      </header>
    );
  }

  return (
    <header className="relative flex items-center h-14 px-4 border-b border-border/70 bg-background shrink-0 gap-3">
      {/* subtle top accent */}
      <div
        aria-hidden
        className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-primary/40 to-transparent"
      />

      {/* Mobile hamburger */}
      <Button
        variant="ghost"
        size="icon"
        className="lg:hidden h-8 w-8 shrink-0"
        onClick={onMenuClick}
        aria-label="Open navigation"
      >
        <Menu size={18} />
      </Button>
      <Separator orientation="vertical" className="h-5 lg:hidden" />

      <div className="flex items-center gap-3 flex-1 min-w-0">
        {Icon && (
          <div className="relative shrink-0">
            <div
              aria-hidden
              className="absolute inset-0 rounded-xl bg-primary/15 blur-md"
            />
            <div className="relative w-9 h-9 rounded-xl flex items-center justify-center bg-gradient-to-br from-primary/15 to-primary/5 border border-primary/20 text-primary shadow-sm">
              <Icon size={18} strokeWidth={2.25} />
            </div>
          </div>
        )}
        <div className="flex flex-col min-w-0 leading-tight">
          <h1 className="font-heading text-[17px] font-semibold tracking-tight text-transparent bg-clip-text bg-gradient-to-br from-foreground to-foreground/70 truncate">
            {meta?.title ?? "Beever Atlas"}
          </h1>
          {meta?.subtitle && (
            <p className="text-[11px] font-medium uppercase tracking-[0.14em] text-muted-foreground/70 truncate">
              {meta.subtitle}
            </p>
          )}
        </div>
      </div>

      {/* Profile avatar button */}
      <NavLink
        to="/profile"
        title={avatarTitle}
        className={({ isActive }) =>
          cn(
            "relative w-8 h-8 rounded-xl flex items-center justify-center text-white text-xs font-bold shrink-0",
            "transition-all duration-150 hover:scale-105 hover:shadow-md",
            "ring-offset-background",
            isActive ? "ring-2 ring-primary ring-offset-1" : ""
          )
        }
        style={{ background: profile.avatarColor || "hsl(215, 80%, 55%)" }}
        aria-label="My profile"
      >
        {emoji}
      </NavLink>
    </header>
  );
}
