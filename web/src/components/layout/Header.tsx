import { useLocation } from "react-router-dom";
import { Menu } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Separator } from "@/components/ui/separator";

interface HeaderProps {
  onMenuClick: () => void;
}

const PAGE_TITLES: Record<string, string> = {
  "/": "Dashboard",
  "/channels": "Channels",
  "/search": "Search",
  "/graph": "Graph Explorer",
  "/settings": "Settings",
};

export function Header({ onMenuClick }: HeaderProps) {
  const location = useLocation();
  const isChannel = location.pathname.startsWith("/channels/");
  const title = PAGE_TITLES[location.pathname];

  if (isChannel) {
    // Keep only a lightweight mobile top bar on channel screens.
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
      <h1 className="text-base font-semibold text-foreground">
        {title ?? "Beever Atlas"}
      </h1>
    </header>
  );
}
