import { useState, useEffect, useMemo } from "react";
import { Link } from "react-router-dom";
import { api } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { Search, Plus, Hash } from "lucide-react";
import { useTheme } from "@/hooks/useTheme";
import { getPlatformBadgeStyle } from "@/lib/platform-badge";
import { cn } from "@/lib/utils";

interface Channel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
  topic: string | null;
  purpose: string | null;
}

type StatusFilter = "all" | "connected" | "not_connected";

export function Channels() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loading, setLoading] = useState(true);
  const [query, setQuery] = useState("");
  const [platformFilter, setPlatformFilter] = useState<string>("all");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all");
  const { resolvedTheme } = useTheme();
  const isDark = resolvedTheme === "dark";

  useEffect(() => {
    api
      .get<Channel[]>("/api/channels")
      .then(setChannels)
      .catch(() => setChannels([]))
      .finally(() => setLoading(false));
  }, []);

  const platforms = useMemo(
    () => [...new Set(channels.map((ch) => ch.platform))],
    [channels],
  );

  const connectedCount = useMemo(
    () => channels.filter((ch) => ch.is_member).length,
    [channels],
  );

  const filtered = useMemo(() => {
    let result = channels;

    // Text search
    if (query.trim()) {
      const q = query.toLowerCase();
      result = result.filter(
        (ch) =>
          ch.name.toLowerCase().includes(q) ||
          ch.platform.toLowerCase().includes(q) ||
          ch.topic?.toLowerCase().includes(q) ||
          ch.purpose?.toLowerCase().includes(q)
      );
    }

    // Platform filter
    if (platformFilter !== "all") {
      result = result.filter((ch) => ch.platform === platformFilter);
    }

    // Status filter
    if (statusFilter === "connected") {
      result = result.filter((ch) => ch.is_member);
    } else if (statusFilter === "not_connected") {
      result = result.filter((ch) => !ch.is_member);
    }

    // Sort: connected first, then alphabetically
    return [...result].sort((a, b) => {
      if (a.is_member !== b.is_member) return a.is_member ? -1 : 1;
      return a.name.localeCompare(b.name);
    });
  }, [channels, query, platformFilter, statusFilter]);

  return (
    <div className="min-h-full">
      <div className="max-w-6xl mx-auto px-4 py-6 sm:px-6 sm:py-8 lg:px-12 lg:py-12">
      {/* Header */}
      <div className="flex flex-col sm:flex-row sm:items-center sm:justify-between gap-3 sm:gap-4 mb-5 sm:mb-6">
        <div>
          <h1 className="font-heading text-2xl sm:text-[32px] tracking-tight text-foreground">Channels</h1>
          {!loading && (
            <p className="text-sm sm:text-base text-muted-foreground mt-1">
              {connectedCount} connected · {channels.length} total
            </p>
          )}
        </div>
        <button className="inline-flex items-center justify-center gap-2 bg-primary text-primary-foreground rounded-full px-5 py-2.5 text-sm sm:text-[15px] font-medium hover:bg-primary/90 transition-colors w-full sm:w-auto">
          <Plus className="w-4 h-4" />
          Connect Channel
        </button>
      </div>

      {/* Search bar */}
      <div className="flex flex-col gap-2.5 sm:gap-3 mb-5 sm:mb-6">
        <div className="relative w-full sm:max-w-sm">
          <Search className="absolute left-3.5 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
          <input
            type="text"
            placeholder="Search channels..."
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            className="w-full pl-10 pr-4 py-2 sm:py-2.5 rounded-2xl bg-card border border-border text-sm sm:text-base text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
          />
        </div>

        {/* Filter pills — horizontal scroll on mobile */}
        <div className="overflow-x-auto no-scrollbar -mx-4 px-4 sm:mx-0 sm:px-0">
          <div className="flex gap-1.5 sm:gap-2 min-w-max sm:min-w-0 sm:flex-wrap">
            {/* Status filters */}
            {(["all", "connected", "not_connected"] as const).map((status) => {
              const labels: Record<StatusFilter, string> = {
                all: "All",
                connected: "Connected",
                not_connected: "Not Connected",
              };
              return (
                <button
                  key={status}
                  onClick={() => setStatusFilter(status)}
                  className={cn(
                    "px-3 sm:px-3.5 py-1 sm:py-1.5 rounded-full text-xs sm:text-sm font-medium transition-colors border whitespace-nowrap",
                    statusFilter === status
                      ? "bg-primary/10 text-primary border-primary/20"
                      : "bg-card text-muted-foreground border-border hover:bg-muted"
                  )}
                >
                  {labels[status]}
                </button>
              );
            })}

            {/* Separator */}
            {platforms.length > 1 && (
              <span className="w-px h-6 sm:h-7 bg-border self-center mx-0.5 sm:mx-1" />
            )}

            {/* Platform filters */}
            {platforms.length > 1 &&
              ["all", ...platforms].map((platform) => (
                <button
                  key={platform}
                  onClick={() => setPlatformFilter(platform)}
                  className={cn(
                    "px-3 sm:px-3.5 py-1 sm:py-1.5 rounded-full text-xs sm:text-sm font-medium transition-colors border capitalize whitespace-nowrap",
                    platformFilter === platform
                      ? "bg-primary/10 text-primary border-primary/20"
                      : "bg-card text-muted-foreground border-border hover:bg-muted"
                  )}
                >
                  {platform === "all" ? "All Platforms" : platform}
                </button>
              ))}
          </div>
        </div>
      </div>

      {/* Grid */}
      {loading ? (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {Array.from({ length: 9 }).map((_, i) => (
            <div key={i} className="bg-card rounded-2xl border border-border p-5 flex flex-col gap-3">
              <Skeleton className="h-5 w-32" />
              <Skeleton className="h-4 w-16" />
              <Skeleton className="h-3 w-full" />
              <Skeleton className="h-3 w-3/4" />
              <Skeleton className="h-3 w-24" />
            </div>
          ))}
        </div>
      ) : filtered.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-20 text-center">
          <Hash className="w-8 h-8 text-muted-foreground/30 mb-3" />
          <p className="text-base font-medium text-foreground">
            {query ? "No channels match your search" : "No channels found"}
          </p>
          <p className="text-sm text-muted-foreground mt-1">
            {query
              ? "Try a different keyword or clear the search."
              : "Connect a platform or enable mock mode to get started."}
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {filtered.map((ch, idx) => (
            <Link
              key={ch.channel_id}
              to={`/channels/${ch.channel_id}/wiki`}
              state={{
                channel_name: ch.name,
                platform: ch.platform,
                is_member: ch.is_member,
                member_count: ch.member_count,
              }}
              className={cn(
                "bg-card rounded-2xl border p-4 sm:p-5 flex flex-col gap-2.5 sm:gap-3 hover:shadow-sm transition-shadow group motion-safe:animate-rise-in",
                ch.is_member ? "border-border" : "border-dashed border-border/60"
              )}
              style={{ animationDelay: `${Math.min(idx, 8) * 45}ms` }}
            >
              <div className="flex items-center gap-2 min-w-0">
                <span className={cn(
                  "w-2 h-2 rounded-full shrink-0",
                  ch.is_member ? "bg-emerald-500" : "bg-muted-foreground/30"
                )} />
                <span className="text-base sm:text-lg font-semibold text-primary shrink-0">#</span>
                <span className={cn(
                  "text-sm sm:text-base font-medium truncate group-hover:text-primary transition-colors",
                  ch.is_member ? "text-foreground" : "text-muted-foreground"
                )}>
                  {ch.name}
                </span>
              </div>
              <div className="flex items-center gap-1.5 sm:gap-2 flex-wrap">
                <span
                  className="inline-flex w-fit px-2 sm:px-2.5 py-0.5 rounded-xl text-[11px] sm:text-xs font-medium capitalize"
                  style={getPlatformBadgeStyle(ch.platform, isDark)}
                >
                  {ch.platform}
                </span>
                <span
                  className={cn(
                    "inline-flex w-fit px-2 sm:px-2.5 py-0.5 rounded-xl text-[11px] sm:text-xs font-medium",
                    ch.is_member
                      ? "bg-emerald-500/10 text-emerald-600 dark:text-emerald-400"
                      : "bg-amber-500/10 text-amber-600 dark:text-amber-400"
                  )}
                >
                  {ch.is_member ? "Connected" : "Not Connected"}
                </span>
              </div>
              <p className="text-sm text-muted-foreground leading-relaxed line-clamp-2">
                {ch.topic || ch.purpose || "No description"}
              </p>
              {ch.member_count != null && (
                <div className="flex items-center gap-3 text-sm text-muted-foreground/70">
                  <span>{ch.member_count.toLocaleString()} members</span>
                </div>
              )}
            </Link>
          ))}
        </div>
      )}
      </div>
    </div>
  );
}
