import { useLocation, NavLink } from "react-router-dom";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";
import { useUserProfile } from "@/hooks/useUserProfile";
import { cn } from "@/lib/utils";

interface HeaderProps {
  onMenuClick: () => void;
}

const PAGE_TITLES: Record<string, string> = {
  "/": "Home",
  "/channels": "Channels",
  "/ask": "Ask",
  "/settings": "Settings",
  "/activity": "Activity",
  "/profile": "My Profile",
};

export function Header({ onMenuClick }: HeaderProps) {
  const location = useLocation();
  const isChannel = location.pathname.startsWith("/channels/");
  const title = PAGE_TITLES[location.pathname];
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
    <header className="flex items-center h-12 px-4 border-b border-border bg-background shrink-0 gap-3">
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

      <h1 className="text-base font-semibold text-foreground flex-1">
        {title ?? "Beever Atlas"}
      </h1>

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
