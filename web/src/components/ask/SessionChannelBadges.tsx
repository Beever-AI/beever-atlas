import { Hash } from "lucide-react";

interface SessionChannelBadgesProps {
  channelIds: string[];
  /** Map of channelId → display name (optional). Falls back to raw id. */
  channelNames?: Record<string, string>;
  maxVisible?: number;
}

/**
 * Compact row of channel badges for sidebar conversation items.
 * Shows up to `maxVisible` channels then collapses the rest into a +N indicator.
 */
export function SessionChannelBadges({
  channelIds,
  channelNames,
  maxVisible = 2,
}: SessionChannelBadgesProps) {
  if (channelIds.length === 0) return null;

  const visible = channelIds.slice(0, maxVisible);
  const overflow = channelIds.length - visible.length;

  const displayName = (id: string) => {
    const name = channelNames?.[id] ?? id;
    return name.length > 14 ? name.slice(0, 13) + "…" : name;
  };

  return (
    <div className="flex items-center gap-1 flex-wrap">
      {visible.map((id) => (
        <span
          key={id}
          className="inline-flex items-center gap-0.5 text-[9px] px-1 py-0 rounded bg-primary/5 text-primary/80 border border-primary/10 max-w-[100px]"
          title={channelNames?.[id] ?? id}
        >
          <Hash className="w-2 h-2 shrink-0" />
          <span className="truncate">{displayName(id)}</span>
        </span>
      ))}
      {overflow > 0 && (
        <span
          className="text-[9px] px-1 py-0 rounded bg-muted text-muted-foreground"
          title={channelIds.slice(maxVisible).join(", ")}
        >
          +{overflow}
        </span>
      )}
    </div>
  );
}
