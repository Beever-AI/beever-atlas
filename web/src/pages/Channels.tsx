import { useState, useEffect, useMemo } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Search,
  Plus,
  Hash,
  ChevronDown,
  ChevronRight,
  X,
  Users,
  ArrowRight,
} from "lucide-react";
import { useTheme } from "@/hooks/useTheme";
import { useConnectionMap } from "@/hooks/useConnectionMap";
import { getPlatformBadgeStyle } from "@/lib/platform-badge";
import { cn } from "@/lib/utils";
import type { PlatformConnection } from "@/lib/types";

interface Channel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  topic: string | null;
  purpose: string | null;
  connection_id: string | null;
}

interface WorkspaceChannelGroup {
  connection: PlatformConnection;
  channels: Channel[];
}

function groupByWorkspace(
  channels: Channel[],
  connections: PlatformConnection[],
): WorkspaceChannelGroup[] {
  const buckets = new Map<string, Channel[]>();
  for (const ch of channels) {
    const key = ch.connection_id ?? "__ungrouped__";
    const list = buckets.get(key) ?? [];
    list.push(ch);
    buckets.set(key, list);
  }
  const groups: WorkspaceChannelGroup[] = [];
  const seen = new Set<string>();
  for (const conn of connections) {
    const chs = buckets.get(conn.id);
    if (chs && chs.length > 0) {
      groups.push({ connection: conn, channels: chs.sort((a, b) => a.name.localeCompare(b.name)) });
      seen.add(conn.id);
    }
  }
  // Fallback: surface channels whose connection_id has no matching PlatformConnection
  // (e.g. /api/connections returned empty or the connection was deleted). Without
  // this the page renders "No connected channels yet" while 3 channels exist.
  const orphanBuckets: { key: string; chs: Channel[] }[] = [];
  for (const [key, chs] of buckets.entries()) {
    if (!seen.has(key) && chs.length > 0) orphanBuckets.push({ key, chs });
  }
  for (const { key, chs } of orphanBuckets) {
    groups.push({
      connection: {
        id: key,
        platform: chs[0]?.platform ?? "unknown",
        display_name: "Ungrouped",
        workspace_name: "Ungrouped",
        is_active: true,
      } as unknown as PlatformConnection,
      channels: chs.sort((a, b) => a.name.localeCompare(b.name)),
    });
  }
  return groups;
}

export function Channels() {
  const [searchParams] = useSearchParams();
  const initialWorkspace = searchParams.get("workspace") ?? "all";
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [workspaceFilter, setWorkspaceFilter] = useState<string>(initialWorkspace);
  const [showAvailable, setShowAvailable] = useState(false);
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";
  const { connections, getWorkspaceName } = useConnectionMap();

  useEffect(() => {
    api
      .get<Channel[]>("/api/channels")
      .then(setChannels)
      .catch(() => setChannels([]))
      .finally(() => setLoading(false));
  }, []);

  const connectedCount = useMemo(() => channels.filter((ch) => ch.is_member).length, [channels]);
  const availableCount = useMemo(() => channels.filter((ch) => !ch.is_member).length, [channels]);

  const { connectedGroups, availableGroups } = useMemo(() => {
    let conn = channels.filter((ch) => ch.is_member);
    let avail = channels.filter((ch) => !ch.is_member);

    if (workspaceFilter !== "all") {
      conn = conn.filter((ch) => ch.connection_id === workspaceFilter);
      avail = avail.filter((ch) => ch.connection_id === workspaceFilter);
    }
    if (query.trim()) {
      const q = query.toLowerCase();
      const matchFn = (ch: Channel) =>
        ch.name.toLowerCase().includes(q) ||
        ch.topic?.toLowerCase().includes(q) ||
        ch.purpose?.toLowerCase().includes(q) ||
        getWorkspaceName(ch.connection_id).toLowerCase().includes(q);
      conn = conn.filter(matchFn);
      avail = avail.filter(matchFn);
    }
    return { connectedGroups: groupByWorkspace(conn, connections), availableGroups: groupByWorkspace(avail, connections) };
  }, [channels, query, workspaceFilter, connections, getWorkspaceName]);

  const totalConnected = connectedGroups.reduce((s, g) => s + g.channels.length, 0);
  const totalAvailable = availableGroups.reduce((s, g) => s + g.channels.length, 0);
  const hasFilter = workspaceFilter !== "all" || query.trim() !== "";

  return (
    <div className="h-full overflow-auto">
      <div className="max-w-5xl mx-auto px-4 py-6 sm:px-6 sm:py-8 lg:px-10 lg:py-10">

        {/* Header */}
        <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-4 mb-8">
          <div>
            <h1 className="font-heading text-2xl sm:text-3xl tracking-tight text-foreground">Channels</h1>
            {!loading && (
              <div className="flex items-center gap-3 mt-2 text-sm text-muted-foreground">
                <span className="flex items-center gap-1.5">
                  <span className="w-2 h-2 rounded-full bg-emerald-500" />
                  {connectedCount} connected
                </span>
                <span className="text-border">·</span>
                <span>{availableCount} available</span>
              </div>
            )}
          </div>
          <Link
            to="/settings"
            className="inline-flex items-center justify-center gap-2 bg-primary text-primary-foreground rounded-lg px-4 py-2 text-sm font-medium hover:bg-primary/90 transition-colors shrink-0"
          >
            <Plus className="w-4 h-4" />
            Add Connection
          </Link>
        </div>

        {/* Toolbar */}
        <div className="bg-card border border-border rounded-xl p-3 mb-8">
          <div className="flex flex-col sm:flex-row gap-3 items-stretch sm:items-center">
            <div className="relative flex-1">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
              <input
                type="text"
                placeholder="Search channels..."
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                className="w-full pl-9 pr-8 py-2 rounded-lg bg-background border border-border text-sm text-foreground placeholder:text-muted-foreground/50 focus:outline-none focus:ring-2 focus:ring-primary/20 focus:border-primary/30 transition-colors"
              />
              {query && (
                <button onClick={() => setQuery("")} className="absolute right-2.5 top-1/2 -translate-y-1/2 p-0.5 rounded hover:bg-muted text-muted-foreground">
                  <X size={14} />
                </button>
              )}
            </div>
            {connections.length > 1 && (
              <div className="flex gap-1 overflow-x-auto no-scrollbar shrink-0">
                <button
                  onClick={() => setWorkspaceFilter("all")}
                  className={cn(
                    "px-3 py-1.5 rounded-lg text-xs font-medium transition-all whitespace-nowrap",
                    workspaceFilter === "all" ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:text-foreground hover:bg-muted"
                  )}
                >
                  All
                </button>
                {connections.map((c) => (
                  <button
                    key={c.id}
                    onClick={() => setWorkspaceFilter(c.id)}
                    className={cn(
                      "inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-all whitespace-nowrap",
                      workspaceFilter === c.id ? "bg-primary text-primary-foreground shadow-sm" : "text-muted-foreground hover:text-foreground hover:bg-muted"
                    )}
                  >
                    {c.display_name}
                    <span
                      className={cn("px-1.5 rounded text-[10px] font-medium capitalize", workspaceFilter === c.id ? "bg-primary-foreground/20 text-primary-foreground" : "")}
                      style={workspaceFilter === c.id ? undefined : getPlatformBadgeStyle(c.platform, isDark)}
                    >
                      {c.platform}
                    </span>
                  </button>
                ))}
              </div>
            )}
          </div>
          {hasFilter && (
            <div className="flex items-center gap-2 mt-2 pt-2 border-t border-border text-xs text-muted-foreground">
              <span>{totalConnected + totalAvailable} results{workspaceFilter !== "all" && ` in ${getWorkspaceName(workspaceFilter)}`}{query && ` for "${query}"`}</span>
              <button onClick={() => { setWorkspaceFilter("all"); setQuery(""); }} className="text-primary hover:underline font-medium">Clear</button>
            </div>
          )}
        </div>

        {/* Content */}
        {loading ? (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
            {Array.from({ length: 6 }).map((_, i) => (
              <div key={i} className="bg-card rounded-xl border border-border p-5 space-y-3">
                <Skeleton className="h-5 w-24" />
                <Skeleton className="h-3 w-full" />
                <Skeleton className="h-3 w-2/3" />
              </div>
            ))}
          </div>
        ) : (
          <>
            {/* ── Connected: Card Grid ── */}
            <section className="mb-10">
              <SectionLabel label="Connected" count={totalConnected} dotColor="bg-emerald-500" />

              {connectedGroups.length === 0 ? (
                <EmptyBlock message={hasFilter ? "No connected channels match" : "No connected channels yet"} actionTo={!hasFilter ? "/settings" : undefined} actionLabel="Add a connection" />
              ) : (
                <div className="space-y-8">
                  {connectedGroups.map((group) => (
                    <div key={group.connection.id}>
                      <WorkspaceHeader connection={group.connection} count={group.channels.length} isDark={isDark} />
                      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-3">
                        {group.channels.map((ch, idx) => (
                          <Link
                            key={ch.channel_id}
                            to={`/channels/${ch.channel_id}/wiki`}
                            state={{ channel_name: ch.name, platform: ch.platform, is_member: ch.is_member, member_count: ch.member_count, connection_id: ch.connection_id }}
                            className="bg-card rounded-xl border border-border p-4 flex flex-col justify-between hover:border-primary/30 hover:shadow-[0_0_0_1px_hsl(var(--primary)/0.1)] transition-all group motion-safe:animate-rise-in h-[120px]"
                            style={{ animationDelay: `${Math.min(idx, 8) * 40}ms` }}
                          >
                            <div>
                              <div className="flex items-center gap-2 mb-1.5">
                                <Hash size={15} className="text-primary/60 shrink-0" />
                                <span className="text-sm font-semibold text-foreground group-hover:text-primary transition-colors truncate">
                                  {ch.name}
                                </span>
                              </div>
                              <p className="text-xs text-muted-foreground/60 leading-relaxed line-clamp-2 pl-[23px]">
                                {ch.topic || ch.purpose || "No description"}
                              </p>
                            </div>
                            <div className="flex items-center justify-between pt-2 pl-[23px]">
                              {ch.member_count != null ? (
                                <span className="inline-flex items-center gap-1 text-[11px] text-muted-foreground/40 tabular-nums">
                                  <Users size={10} />
                                  {ch.member_count} members
                                </span>
                              ) : <span />}
                              <ArrowRight size={14} className="text-muted-foreground/0 group-hover:text-primary/50 transition-colors" />
                            </div>
                          </Link>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>

            {/* ── Available: Compact List ── */}
            <section>
              <button onClick={() => setShowAvailable(!showAvailable)} className="flex items-center gap-2 mb-4 group w-full">
                {showAvailable ? <ChevronDown size={14} className="text-muted-foreground/60" /> : <ChevronRight size={14} className="text-muted-foreground/60" />}
                <SectionLabel label="Available" count={totalAvailable} dotColor="bg-muted-foreground/30" inline />
              </button>

              {showAvailable && (
                availableGroups.length === 0 ? (
                  <EmptyBlock message={hasFilter ? "No available channels match" : "All channels are connected"} />
                ) : (
                  <div className="space-y-6">
                    {availableGroups.map((group) => (
                      <div key={group.connection.id}>
                        <WorkspaceHeader connection={group.connection} count={group.channels.length} isDark={isDark} />
                        <div className="rounded-lg border border-dashed border-border/60 overflow-hidden">
                          {group.channels.map((ch, i) => (
                            <Link
                              key={ch.channel_id}
                              to={`/channels/${ch.channel_id}`}
                              state={{ channel_name: ch.name, platform: ch.platform, is_member: ch.is_member, member_count: ch.member_count, connection_id: ch.connection_id }}
                              className={cn(
                                "flex items-center gap-3 px-4 py-2.5 hover:bg-muted/30 transition-colors group",
                                i > 0 && "border-t border-dashed border-border/40"
                              )}
                            >
                              <Hash size={13} className="text-muted-foreground/30 shrink-0" />
                              <span className="text-sm text-muted-foreground group-hover:text-foreground transition-colors truncate">
                                {ch.name}
                              </span>
                              {(ch.topic || ch.purpose) && (
                                <span className="text-xs text-muted-foreground/30 truncate hidden sm:inline flex-1 min-w-0">
                                  {ch.topic || ch.purpose}
                                </span>
                              )}
                              {ch.member_count != null && (
                                <span className="text-[11px] text-muted-foreground/30 tabular-nums shrink-0 hidden sm:inline">
                                  {ch.member_count}
                                </span>
                              )}
                            </Link>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )
              )}
            </section>
          </>
        )}
      </div>
    </div>
  );
}

/* ── Sub-components ── */

function SectionLabel({ label, count, dotColor, inline }: { label: string; count: number; dotColor: string; inline?: boolean }) {
  return (
    <div className={cn("flex items-center gap-2", !inline && "mb-5")}>
      <span className={cn("w-2 h-2 rounded-full shrink-0", dotColor)} />
      <h2 className="text-xs font-semibold tracking-wide text-muted-foreground uppercase">{label}</h2>
      <span className="text-xs text-muted-foreground/50 tabular-nums">{count}</span>
    </div>
  );
}

function WorkspaceHeader({ connection, count, isDark }: { connection: PlatformConnection; count: number; isDark: boolean }) {
  return (
    <div className="flex items-center gap-2.5 mb-3">
      <span className="font-heading text-base font-medium text-foreground">{connection.display_name}</span>
      <span className="inline-flex px-2 py-0.5 rounded-md text-[11px] font-medium capitalize" style={getPlatformBadgeStyle(connection.platform, isDark)}>
        {connection.platform}
      </span>
      <span className="flex-1 h-px bg-border" />
      <span className="text-xs text-muted-foreground/40 tabular-nums">{count}</span>
    </div>
  );
}

function EmptyBlock({ message, actionTo, actionLabel }: { message: string; actionTo?: string; actionLabel?: string }) {
  return (
    <div className="rounded-xl border border-dashed border-border py-10 text-center">
      <Hash className="w-5 h-5 text-muted-foreground/20 mx-auto mb-2" />
      <p className="text-sm text-muted-foreground/60">{message}</p>
      {actionTo && actionLabel && (
        <Link to={actionTo} className="inline-flex items-center gap-1.5 mt-3 text-sm text-primary hover:underline font-medium">
          {actionLabel} <ArrowRight size={14} />
        </Link>
      )}
    </div>
  );
}
