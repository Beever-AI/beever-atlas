import type { WikiState, WikiStateEntry } from "@/hooks/useWikiStates";

/** Tier order used when sorting channel lists (sidebar, picker). Lower wins. */
export const WIKI_STATE_TIER: Record<WikiState, number> = {
  ready: 0,
  building: 1,
  empty: 2,
  errored: 3,
};

/** Compare fn that puts wiki-ready channels first, then alphabetical. */
export function compareChannelsByWikiState<
  T extends { channel_id: string; name: string },
>(
  a: T,
  b: T,
  getState: (channelId: string) => WikiState,
): number {
  const ta = WIKI_STATE_TIER[getState(a.channel_id)];
  const tb = WIKI_STATE_TIER[getState(b.channel_id)];
  if (ta !== tb) return ta - tb;
  return a.name.localeCompare(b.name);
}

/** Coverage summary used in workspace headers and ChannelPicker. */
export function summarizeWikiCoverage<T extends { channel_id: string }>(
  channels: T[],
  getState: (channelId: string) => WikiState,
): { ready: number; building: number; empty: number; total: number } {
  let ready = 0;
  let building = 0;
  let empty = 0;
  for (const ch of channels) {
    const s = getState(ch.channel_id);
    if (s === "ready") ready++;
    else if (s === "building") building++;
    else empty++; // treat "errored" like "empty" for the summary
  }
  return { ready, building, empty, total: channels.length };
}

/** Short relative-time label (e.g. "2h ago", "3d ago"). */
export function formatRelativeTime(timestamp: string | null | undefined): string {
  if (!timestamp) return "";
  const t = new Date(timestamp).getTime();
  if (!Number.isFinite(t)) return "";
  const diffMs = Date.now() - t;
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** Human label for a wiki state — surfaced in tooltips. */
export function wikiStateLabel(state: WikiState, entry?: WikiStateEntry): string {
  switch (state) {
    case "ready": {
      const when = entry?.last_sync_ts ? ` · synced ${formatRelativeTime(entry.last_sync_ts)}` : "";
      return `Wiki ready${when}`;
    }
    case "building":
      return "Building wiki…";
    case "errored":
      return "Wiki errored";
    case "empty":
    default:
      return "No wiki yet";
  }
}
