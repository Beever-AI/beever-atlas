import { useState, useMemo, type ReactNode } from "react";
import { Search, CheckSquare, Square, Hash } from "lucide-react";
import { cn } from "@/lib/utils";
import type { AvailableChannel } from "@/lib/types";

interface ChannelSelectorProps {
  channels: AvailableChannel[];
  selected: string[];
  onChange: (selected: string[]) => void;
  /** Platform of the parent connection. Drives the membership hint copy
   *  — each platform has a different mechanism for "the bot can't see
   *  this channel until X happens" and the hint must match what the user
   *  has to actually do. Optional so legacy callers fall back to a
   *  platform-agnostic line. */
  platform?: string;
}

/** Platform-specific guidance shown under the channel-selector search box.
 *  Each branch tells the user exactly what to do when a channel they expect
 *  to see is missing. Centralised here so it stays in lockstep with the
 *  list of supported platforms in `connections.py:create_connection`. */
function membershipHint(platform: string | undefined): ReactNode {
  const code = (text: string) => (
    <code className="text-[11px] bg-muted px-1 py-0.5 rounded">{text}</code>
  );
  switch (platform) {
    case "slack":
      return (
        <>
          Only channels where the bot is a member are shown. Invite the bot with{" "}
          {code("/invite @beever")} in Slack to add more.
        </>
      );
    case "discord":
      return (
        <>
          Only text channels in servers the bot has joined are shown. Add the bot
          to a server via its OAuth invite, then re-open this dialog to refresh.
        </>
      );
    case "mattermost":
      return (
        <>
          Only channels the bot is a member of are shown. Invite the bot with{" "}
          {code("/invite @beever")} in Mattermost to add more.
        </>
      );
    case "teams":
      return (
        <>
          Only channels in teams where the bot has been installed are shown. A
          Teams admin installs Beever Atlas via Teams Admin Center →{" "}
          <strong>Manage apps</strong>; per-channel invites are not required.
        </>
      );
    case "telegram":
      return (
        <>
          Channels appear here once the bot has received any message from the
          chat. Add the bot to a Telegram group/channel and post one message,
          then refresh.
        </>
      );
    default:
      return <>Only channels where the bot is a member are shown.</>;
  }
}

export function ChannelSelector({ channels, selected, onChange, platform }: ChannelSelectorProps) {
  const [search, setSearch] = useState("");

  const filtered = useMemo(() => {
    const q = search.toLowerCase().trim();
    if (!q) return channels;
    return channels.filter(
      (c) =>
        c.name.toLowerCase().includes(q) ||
        (c.topic ?? "").toLowerCase().includes(q) ||
        (c.purpose ?? "").toLowerCase().includes(q),
    );
  }, [channels, search]);

  const allSelected = filtered.length > 0 && filtered.every((c) => selected.includes(c.channel_id));

  function toggleAll() {
    if (allSelected) {
      const filteredIds = new Set(filtered.map((c) => c.channel_id));
      onChange(selected.filter((id) => !filteredIds.has(id)));
    } else {
      const toAdd = filtered.map((c) => c.channel_id).filter((id) => !selected.includes(id));
      onChange([...selected, ...toAdd]);
    }
  }

  function toggleOne(channelId: string) {
    if (selected.includes(channelId)) {
      onChange(selected.filter((id) => id !== channelId));
    } else {
      onChange([...selected, channelId]);
    }
  }

  return (
    <div className="flex flex-col gap-3">
      {/* Search */}
      <div className="relative">
        <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground pointer-events-none" />
        <input
          type="text"
          placeholder="Search channels..."
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="w-full h-9 pl-9 pr-3 rounded-lg border border-border bg-background text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-primary/20"
        />
      </div>

      {/* Hint about bot membership — platform-specific copy so each
       *  workspace tells the user what they actually need to do when a
       *  channel they expect to see is missing. */}
      <p className="text-xs text-muted-foreground px-1">
        {membershipHint(platform)}
      </p>

      {/* Select all / deselect all */}
      <div className="flex items-center justify-between px-1">
        <span className="text-xs text-muted-foreground">
          {selected.length} of {channels.length} selected
        </span>
        <button
          type="button"
          onClick={toggleAll}
          className="text-xs text-primary hover:underline"
        >
          {allSelected ? "Deselect all" : "Select all"}
        </button>
      </div>

      {/* Channel list */}
      <div className="border border-border rounded-xl overflow-hidden divide-y divide-border max-h-64 overflow-y-auto">
        {filtered.length === 0 ? (
          <div className="px-4 py-6 text-center text-sm text-muted-foreground">No channels found</div>
        ) : (
          filtered.map((channel) => {
            const isSelected = selected.includes(channel.channel_id);
            return (
              <button
                key={channel.channel_id}
                type="button"
                onClick={() => toggleOne(channel.channel_id)}
                className={cn(
                  "w-full flex items-start gap-3 px-4 py-3 text-left transition-colors hover:bg-muted/50",
                  isSelected && "bg-primary/5",
                )}
              >
                <div className="mt-0.5 shrink-0 text-primary">
                  {isSelected ? (
                    <CheckSquare className="w-4 h-4" />
                  ) : (
                    <Square className="w-4 h-4 text-muted-foreground" />
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-1.5">
                    <Hash className="w-3.5 h-3.5 text-muted-foreground shrink-0" />
                    <span className="text-sm font-medium text-foreground truncate">{channel.name}</span>
                    {channel.member_count != null && (
                      <span className="text-xs text-muted-foreground ml-auto shrink-0">
                        {channel.member_count.toLocaleString()} members
                      </span>
                    )}
                  </div>
                  {(channel.topic || channel.purpose) && (
                    <p className="text-xs text-muted-foreground mt-0.5 truncate">
                      {channel.topic || channel.purpose}
                    </p>
                  )}
                </div>
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}
