import { useState } from "react";
import { NavLink } from "react-router-dom";
import { Hash, ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { PlatformIcon } from "@/components/shared/PlatformIcon";
import { FavoriteButton } from "./FavoriteButton";

interface Channel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  connection_id: string | null;
}

interface WorkspaceGroupProps {
  label: string;
  platform: string;
  channels: Channel[];
  defaultCollapsed: boolean;
  onToggleCollapse: () => void;
  isFavorite: (channelId: string) => boolean;
  onToggleFavorite: (channel: { channel_id: string; connection_id: string | null }) => void;
  showWorkspaceName?: boolean;
}

export function WorkspaceGroup({
  label,
  platform,
  channels,
  defaultCollapsed,
  onToggleCollapse,
  isFavorite,
  onToggleFavorite,
  showWorkspaceName,
}: WorkspaceGroupProps) {
  const [collapsed, setCollapsed] = useState(defaultCollapsed);

  const handleToggle = () => {
    setCollapsed(!collapsed);
    onToggleCollapse();
  };

  // Sort: member first, then alphabetically
  const sorted = [...channels].sort((a, b) => {
    if (a.is_member !== b.is_member) return a.is_member ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  return (
    <div className="px-2 pb-1">
      <button
        onClick={handleToggle}
        className="flex items-center gap-1.5 w-full px-2 py-2 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/60 hover:text-foreground transition-colors rounded-lg hover:bg-muted/50"
      >
        {collapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
        <PlatformIcon platform={platform} className="w-3.5 h-3.5 shrink-0 opacity-60" />
        <span className="truncate">{label}</span>
        <span className="ml-auto bg-muted/80 text-muted-foreground/60 text-[10px] font-medium tabular-nums px-1.5 py-0.5 rounded-full">{channels.length}</span>
      </button>

      {!collapsed &&
        sorted.map((ch) => (
          <Tooltip key={ch.channel_id}>
            <TooltipTrigger
              render={
                <NavLink
                  to={`/channels/${ch.channel_id}`}
                  state={{
                    channel_name: ch.name,
                    platform: ch.platform,
                    is_member: ch.is_member,
                    member_count: ch.member_count,
                    connection_id: ch.connection_id,
                  }}
                  className={({ isActive }) =>
                    cn(
                      "flex items-center gap-1.5 px-2 py-1 rounded-lg text-[13px] transition-colors group ml-1",
                      isActive
                        ? "bg-primary/10 text-primary dark:bg-primary/15 dark:text-primary font-medium"
                        : "text-muted-foreground hover:text-foreground hover:bg-muted/60"
                    )
                  }
                >
                  <Hash size={13} className="shrink-0 opacity-30" />
                  <span className={cn("truncate flex-1", !ch.is_member && "opacity-60")}>
                    {ch.name}
                  </span>
                  {showWorkspaceName && (
                    <span className="text-[10px] text-muted-foreground/50 truncate max-w-[60px] shrink-0">
                      {label}
                    </span>
                  )}
                  <FavoriteButton
                    isFavorite={isFavorite(ch.channel_id)}
                    onToggle={() => onToggleFavorite({ channel_id: ch.channel_id, connection_id: ch.connection_id })}
                  />
                </NavLink>
              }
            />
            <TooltipContent side="right" className="text-xs">
              {ch.name}
            </TooltipContent>
          </Tooltip>
        ))}
    </div>
  );
}
