import { useState, useEffect } from "react";
import { NavLink } from "react-router-dom";
import { api } from "@/lib/api";
import { Skeleton } from "@/components/ui/skeleton";
import { Hash } from "lucide-react";
import { cn } from "@/lib/utils";

interface Channel {
  channel_id: string;
  name: string;
  platform: string;
  is_member: boolean;
  member_count: number | null;
}


export function ChannelList() {
  const [channels, setChannels] = useState<Channel[]>([]);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    api
      .get<Channel[]>("/api/channels")
      .then(setChannels)
      .catch(() => setChannels([]))
      .finally(() => setLoading(false));
  }, []);

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

  // Sort: member channels first, then alphabetically within each group
  const sorted = [...channels].sort((a, b) => {
    if (a.is_member !== b.is_member) return a.is_member ? -1 : 1;
    return a.name.localeCompare(b.name);
  });

  if (channels.length === 0) {
    return (
      <div className="px-4 py-3">
        <p className="text-sm text-muted-foreground">
          No channels. Connect a platform in Settings.
        </p>
      </div>
    );
  }

  return (
    <div className="px-2 py-1">
      <p className="px-2 py-1 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
        Channels
      </p>
      {sorted.map((ch) => (
        <NavLink
          key={ch.channel_id}
          to={`/channels/${ch.channel_id}`}
          state={{
            channel_name: ch.name,
            platform: ch.platform,
            is_member: ch.is_member,
            member_count: ch.member_count,
          }}
          className={({ isActive }) =>
            cn(
              "flex items-center gap-2 px-2 py-1.5 rounded-md text-sm transition-colors group",
              isActive
                ? "bg-primary/10 text-primary dark:bg-primary/15 dark:text-primary font-medium"
                : "text-muted-foreground hover:text-foreground hover:bg-muted/60"
            )
          }
        >
          {/* Status dot — green for member, gray for non-member */}
          <span className={cn(
            "w-2 h-2 rounded-full shrink-0",
            ch.is_member ? "bg-emerald-500" : "bg-muted-foreground/30"
          )} />
          {/* Hash icon */}
          <Hash size={14} className="shrink-0 opacity-50" />
          <span className={cn("truncate flex-1", !ch.is_member && "opacity-60")}>{ch.name}</span>
          {ch.member_count != null && (
            <span className="ml-auto text-xs text-muted-foreground/60 tabular-nums shrink-0">
              {ch.member_count}
            </span>
          )}
        </NavLink>
      ))}
    </div>
  );
}
