import { useState, useEffect, useCallback, useMemo, Fragment } from "react";
import { api } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { RefreshCw } from "lucide-react";
import { useConnectionMap } from "@/hooks/useConnectionMap";
import { useFavorites } from "@/hooks/useFavorites";
import { useWikiStates } from "@/hooks/useWikiStates";
import { SidebarSearch } from "./SidebarSearch";
import { FavoritesList } from "./FavoritesList";
import { WorkspaceGroup } from "./WorkspaceGroup";
import { Separator } from "@/components/ui/separator";

interface Channel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  connection_id: string | null;
  /** Status of the parent PlatformConnection at fetch time
   *  ("connected" / "disconnected" / "error" / "pending"). null for
   *  orphan channels with no parent connection (CSV-imported, etc.).
   *  Lets the sidebar render a "needs reconnection" badge instead of
   *  silently hiding the workspace when status drifts. */
  connection_status?: string | null;
}

const COLLAPSED_KEY = "beever-sidebar-groups";

function readCollapsedState(): Record<string, boolean> {
  try {
    const raw = localStorage.getItem(COLLAPSED_KEY);
    if (!raw) return {};
    return JSON.parse(raw);
  } catch {
    return {};
  }
}

function writeCollapsedState(state: Record<string, boolean>): void {
  try {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify(state));
  } catch {
    // Safari private browsing — silently fail
  }
}

export function ChannelList() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [collapsedGroups, setCollapsedGroups] = useState<Record<string, boolean>>(readCollapsedState);

  const { connections, getWorkspaceName, refetch: refetchConnections } = useConnectionMap();
  const { isFavorite, toggleFavorite } = useFavorites();
  const { getState: getWikiState } = useWikiStates();

  const fetchChannels = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .get<Channel[]>("/api/channels")
      .then(setChannels)
      .catch((err) => {
        setError(err instanceof Error ? err.message : "Failed to load channels");
        setChannels([]);
      })
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchChannels();

    const handleConnectionsChanged = () => {
      fetchChannels();
      refetchConnections();
    };

    window.addEventListener("connections-changed", handleConnectionsChanged);
    return () => window.removeEventListener("connections-changed", handleConnectionsChanged);
  }, [fetchChannels, refetchConnections]);

  // Only show connected (member) channels in sidebar
  const memberChannels = useMemo(
    () => channels.filter((ch) => ch.is_member),
    [channels],
  );

  // Resolve favorite channels to full Channel objects
  const favoriteChannels = useMemo(
    () => memberChannels.filter((ch) => isFavorite(ch.channel_id)),
    [memberChannels, isFavorite],
  );

  // Non-favorite channels grouped by connection
  const workspaceGroups = useMemo(() => {
    const nonFavorites = memberChannels.filter((ch) => !isFavorite(ch.channel_id));
    const groups = new Map<string, Channel[]>();

    for (const ch of nonFavorites) {
      const key = ch.connection_id ?? "__ungrouped__";
      const list = groups.get(key) ?? [];
      list.push(ch);
      groups.set(key, list);
    }

    // Build ordered list: known connections first, ungrouped last
    const ordered: {
      key: string;
      label: string;
      platform: string;
      channels: Channel[];
      connectionStatus?: string | null;
    }[] = [];

    for (const conn of connections) {
      const chs = groups.get(conn.id);
      if (chs && chs.length > 0) {
        ordered.push({
          key: conn.id,
          label: conn.display_name,
          platform: conn.platform,
          channels: chs,
          connectionStatus: conn.status,
        });
        groups.delete(conn.id);
      }
    }

    // Ungrouped channels (null connection_id or unknown connection)
    for (const [key, chs] of groups) {
      if (chs.length > 0) {
        ordered.push({ key, label: "Ungrouped", platform: chs[0]?.platform ?? "unknown", channels: chs });
      }
    }

    return ordered;
  }, [memberChannels, connections, isFavorite]);

  // Filtered results when searching (connected channels only)
  const searchResults = useMemo(() => {
    if (!searchQuery.trim()) return null;
    const q = searchQuery.toLowerCase();
    return memberChannels.filter((ch) =>
      ch.name.toLowerCase().includes(q) ||
      getWorkspaceName(ch.connection_id).toLowerCase().includes(q)
    );
  }, [memberChannels, searchQuery, getWorkspaceName]);

  const handleToggleCollapse = (key: string) => {
    setCollapsedGroups((prev) => {
      const next = { ...prev, [key]: !prev[key] };
      writeCollapsedState(next);
      return next;
    });
  };

  if (loading) {
    return (
      <div className="px-2 py-2 space-y-1">
        <p className="px-2 py-1 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
          Channels
        </p>
        {Array.from({ length: 4 }).map((_, i) => (
          <div key={i} className="flex items-center gap-2 px-2 py-1.5">
            <Skeleton className="w-3 h-3 rounded-full shrink-0" />
            <Skeleton className="h-3 flex-1" />
          </div>
        ))}
      </div>
    );
  }

  if (error) {
    return (
      <div className="px-3 py-3 space-y-2">
        <p className="text-sm text-destructive">{error}</p>
        <button
          onClick={fetchChannels}
          className="inline-flex items-center gap-1.5 text-xs text-muted-foreground hover:text-foreground transition-colors"
        >
          <RefreshCw size={12} />
          Retry
        </button>
      </div>
    );
  }

  if (channels.length === 0) {
    const hasConnections = connections.some((c) => c.status === "connected");
    return (
      <div className="px-4 py-3">
        <p className="text-sm text-muted-foreground">
          {hasConnections
            ? "No channels selected. Manage channels on the Channels page."
            : "No channels yet. Connect a platform to get started."}
        </p>
      </div>
    );
  }

  // Search mode — flat filtered results with workspace names
  if (searchResults) {
    return (
      <div className="py-1">
        <SidebarSearch value={searchQuery} onChange={setSearchQuery} />
        {searchResults.length === 0 ? (
          <p className="px-4 py-2 text-xs text-muted-foreground">No channels match</p>
        ) : (
          <WorkspaceGroup
            key="search-results"
            label="Results"
            platform=""
            channels={searchResults}
            defaultCollapsed={false}
            onToggleCollapse={() => {}}
            isFavorite={isFavorite}
            onToggleFavorite={toggleFavorite}
            showWorkspaceName
            getWikiState={getWikiState}
            disableFold
          />
        )}
      </div>
    );
  }

  // Normal mode — favorites + workspace groups
  return (
    <div className="py-1">
      <SidebarSearch value={searchQuery} onChange={setSearchQuery} />

      <FavoritesList
        channels={favoriteChannels}
        getWorkspaceName={getWorkspaceName}
        onToggleFavorite={toggleFavorite}
        getWikiState={getWikiState}
      />

      {favoriteChannels.length > 0 && workspaceGroups.length > 0 && (
        <Separator className="my-1" />
      )}

      {workspaceGroups.map((group, i) => (
        <Fragment key={group.key}>
          {i > 0 && <Separator className="mx-3 my-1.5 bg-border/50" />}
          <WorkspaceGroup
            label={group.label}
            platform={group.platform}
            channels={group.channels}
            defaultCollapsed={collapsedGroups[group.key] ?? false}
            onToggleCollapse={() => handleToggleCollapse(group.key)}
            isFavorite={isFavorite}
            onToggleFavorite={toggleFavorite}
            connectionStatus={group.connectionStatus}
            getWikiState={getWikiState}
          />
        </Fragment>
      ))}
    </div>
  );
}
