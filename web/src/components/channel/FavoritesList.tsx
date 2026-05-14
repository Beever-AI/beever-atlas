import { NavLink } from "react-router-dom";
import { Star } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { WikiStateIcon } from "@/components/shared/WikiStateIcon";
import type { WikiState } from "@/hooks/useWikiStates";
import { wikiStateLabel } from "@/lib/wikiState";
import { FavoriteButton } from "./FavoriteButton";

interface Channel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  connection_id: string | null;
}

interface FavoritesListProps {
  channels: Channel[];
  getWorkspaceName: (connectionId: string | null) => string;
  onToggleFavorite: (channel: { channel_id: string; connection_id: string | null }) => void;
  /** Optional resolver so favorited channels get the same icon treatment as
   *  workspace rows. When omitted (e.g. before wiki states have loaded) all
   *  rows render with the "ready" icon to avoid a misleading "empty" flash. */
  getWikiState?: (channelId: string) => WikiState;
}

export function FavoritesList({
  channels,
  getWorkspaceName,
  onToggleFavorite,
  getWikiState,
}: FavoritesListProps) {
  if (channels.length === 0) return null;

  return (
    <div className="px-2 pb-1">
      <p className="px-2 py-1.5 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/50 flex items-center gap-1.5">
        <Star size={10} fill="currentColor" className="text-muted-foreground/40" />
        Favorites
      </p>
      {channels.map((ch) => {
        const wikiState = getWikiState ? getWikiState(ch.channel_id) : "ready";
        const isEmpty = wikiState === "empty" || wikiState === "errored";
        return (
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
                      "flex items-center gap-1.5 px-2 py-1 rounded-lg text-[13px] transition-colors group",
                      isActive
                        ? "bg-primary/10 text-primary dark:bg-primary/15 dark:text-primary font-medium"
                        : "text-muted-foreground hover:text-foreground hover:bg-muted/60",
                      isEmpty && "opacity-55"
                    )
                  }
                >
                  <WikiStateIcon state={wikiState} size={13} />
                  <span className="truncate flex-1">{ch.name}</span>
                  <span className="text-[10px] text-muted-foreground/50 truncate max-w-[60px] shrink-0">
                    {getWorkspaceName(ch.connection_id)}
                  </span>
                  <FavoriteButton
                    isFavorite={true}
                    onToggle={() => onToggleFavorite({ channel_id: ch.channel_id, connection_id: ch.connection_id })}
                  />
                </NavLink>
              }
            />
            <TooltipContent side="right" className="text-xs">
              <span className="block font-medium">{ch.name}</span>
              <span className="block text-muted-foreground/70 text-[11px] mt-0.5">
                {wikiStateLabel(wikiState)}
              </span>
            </TooltipContent>
          </Tooltip>
        );
      })}
    </div>
  );
}
