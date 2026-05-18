import { useState } from "react";
import { NavLink } from "react-router-dom";
import { AlertTriangle, ChevronDown, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { Tooltip, TooltipTrigger, TooltipContent } from "@/components/ui/tooltip";
import { PlatformIcon } from "@/components/shared/PlatformIcon";
import { WikiStateIcon } from "@/components/shared/WikiStateIcon";
import type { WikiState } from "@/hooks/useWikiStates";
import { compareChannelsByWikiState, summarizeWikiCoverage, wikiStateLabel } from "@/lib/wikiState";
import { FavoriteButton } from "./FavoriteButton";
import { useSyncStatus } from "@/contexts/SyncStatusContext";

interface Channel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  connection_id: string | null;
  connection_status?: string | null;
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
  /** Status of the parent PlatformConnection. When non-null and not
   *  ``"connected"`` we render a small "needs reconnection" badge in
   *  the workspace header so the user understands why channels under
   *  this label may not be live-syncing — instead of silently hiding
   *  the workspace as the previous behaviour did. */
  connectionStatus?: string | null;
  /** Resolves the wiki state for a given channel_id. When omitted (e.g. on
   *  the search-results render path) we treat every row as "ready" so the
   *  list looks unchanged. */
  getWikiState?: (channelId: string) => WikiState;
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
  connectionStatus,
  getWikiState,
}: WorkspaceGroupProps) {
  const isDisconnected =
    connectionStatus != null && connectionStatus !== "connected";
  const [collapsed, setCollapsed] = useState(defaultCollapsed);
  // Read the cross-cutting sync-status signal so we can paint a pulsing
  // indicator on every channel row currently running a sync — supports
  // concurrent syncs across multiple channels.
  const { syncingChannels } = useSyncStatus();

  const handleToggle = () => {
    setCollapsed(!collapsed);
    onToggleCollapse();
  };

  // Sort: wiki-ready first (then building, then empty), alphabetical within tier.
  // Falls back to original behaviour (member-first + alpha) when no wiki-state
  // resolver is supplied (e.g. the search-results render path).
  const sorted = getWikiState
    ? [...channels].sort((a, b) => compareChannelsByWikiState(a, b, getWikiState))
    : [...channels].sort((a, b) => {
        if (a.is_member !== b.is_member) return a.is_member ? -1 : 1;
        return a.name.localeCompare(b.name);
      });

  const coverage = getWikiState
    ? summarizeWikiCoverage(channels, getWikiState)
    : null;

  return (
    <div className="px-2 pb-1">
      <button
        onClick={handleToggle}
        className="flex items-center gap-1.5 w-full px-2 py-2 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/60 hover:text-foreground transition-colors rounded-lg hover:bg-muted/50"
      >
        {collapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
        <PlatformIcon platform={platform} className="w-3.5 h-3.5 shrink-0 opacity-60" />
        <span className="truncate">{label}</span>
        {isDisconnected && (
          <Tooltip>
            <TooltipTrigger
              render={
                <span
                  data-testid="workspace-disconnected-badge"
                  className="inline-flex items-center gap-0.5 rounded-full bg-amber-500/15 text-amber-600 dark:text-amber-400 text-[9px] font-medium px-1.5 py-0.5 normal-case tracking-normal"
                >
                  <AlertTriangle size={9} />
                  {connectionStatus}
                </span>
              }
            />
            <TooltipContent>
              <span className="text-xs">
                Connection is{" "}
                <span className="font-medium">{connectionStatus}</span>. Channels are visible from your last sync — reconnect on the Channels page to resume live sync.
              </span>
            </TooltipContent>
          </Tooltip>
        )}
        {coverage && coverage.total > 0 ? (
          <Tooltip>
            <TooltipTrigger
              render={
                <span className="ml-auto bg-muted/80 text-muted-foreground/70 text-[10px] font-medium tabular-nums px-1.5 py-0.5 rounded-full normal-case tracking-normal">
                  {coverage.ready} / {coverage.total} wiki
                </span>
              }
            />
            <TooltipContent>
              <span className="text-xs">
                {coverage.ready} of {coverage.total} channels have a wiki built.
              </span>
            </TooltipContent>
          </Tooltip>
        ) : (
          <span className="ml-auto bg-muted/80 text-muted-foreground/60 text-[10px] font-medium tabular-nums px-1.5 py-0.5 rounded-full">{channels.length}</span>
        )}
      </button>

      {!collapsed &&
        sorted.map((ch) => {
          const wikiState = getWikiState ? getWikiState(ch.channel_id) : "ready";
          const isEmpty = wikiState === "empty" || wikiState === "errored";
          // RES-285 / sidebar feedback — light up the row of whichever
          // channel is currently syncing so the user can find it again
          // (especially useful when they've navigated away to another
          // channel mid-sync — the top-nav gate told them "something is
          // syncing" but didn't say where).
          const isCurrentlySyncing = syncingChannels.has(ch.channel_id);
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
                        "flex items-center gap-1.5 px-2 py-1 rounded-lg text-[13px] transition-colors group ml-1",
                        isActive
                          ? "bg-primary/10 text-primary dark:bg-primary/15 dark:text-primary font-medium"
                          : "text-muted-foreground hover:text-foreground hover:bg-muted/60",
                        isEmpty && "opacity-55"
                      )
                    }
                  >
                    {isCurrentlySyncing ? (
                      // Pulsing dot replaces the wiki-state icon while a
                      // sync is running on this channel — it's the
                      // single strongest "where the action is" signal.
                      <span
                        className="inline-flex items-center justify-center w-[13px] h-[13px] shrink-0"
                        aria-label="Syncing"
                        title="Syncing"
                      >
                        <span className="block w-2 h-2 rounded-full bg-primary animate-pulse" />
                      </span>
                    ) : (
                      <WikiStateIcon state={wikiState} size={13} />
                    )}
                    <span
                      className={cn(
                        "truncate flex-1",
                        !ch.is_member && "opacity-60",
                        isCurrentlySyncing && "font-medium text-foreground",
                      )}
                    >
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
                <span className="block font-medium">{ch.name}</span>
                <span className="block text-muted-foreground/70 text-[11px] mt-0.5">
                  {isCurrentlySyncing ? "Syncing now…" : wikiStateLabel(wikiState)}
                </span>
              </TooltipContent>
            </Tooltip>
          );
        })}
    </div>
  );
}
