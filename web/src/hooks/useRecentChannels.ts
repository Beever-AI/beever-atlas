import { useState, useEffect, useCallback } from "react";

const STORAGE_KEY = "beever-recent-channels";
const MAX_RECENT = 8;

export interface RecentChannel {
  channel_id: string;
  name: string;
  platform: string;
  visited_at: string; // ISO timestamp
}

function readRecent(): RecentChannel[] {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (e): e is RecentChannel =>
        typeof e === "object" &&
        e !== null &&
        typeof e.channel_id === "string" &&
        typeof e.name === "string",
    );
  } catch {
    return [];
  }
}

function writeRecent(entries: RecentChannel[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // Safari private browsing or quota — silently ignore.
  }
}

export interface UseRecentChannelsReturn {
  recent: RecentChannel[];
  trackVisit: (ch: Omit<RecentChannel, "visited_at">) => void;
  clearRecent: () => void;
}

/**
 * localStorage-backed list of the user's most-recently-visited channels.
 * Used by the home page "Pick up where you left off" section. Kept entirely
 * client-side — adding server-side tracking would require an auth round-trip
 * on every page navigation, which is overkill for a UX nicety.
 */
export function useRecentChannels(): UseRecentChannelsReturn {
  const [recent, setRecent] = useState<RecentChannel[]>(readRecent);

  // Cross-tab sync — if the user opens a channel in another tab we want
  // the home page to reflect it next time they come back.
  useEffect(() => {
    const handler = (e: StorageEvent) => {
      if (e.key === STORAGE_KEY) setRecent(readRecent());
    };
    window.addEventListener("storage", handler);
    return () => window.removeEventListener("storage", handler);
  }, []);

  const trackVisit = useCallback((ch: Omit<RecentChannel, "visited_at">) => {
    if (!ch.channel_id) return;
    setRecent((prev) => {
      const filtered = prev.filter((e) => e.channel_id !== ch.channel_id);
      const next: RecentChannel[] = [
        { ...ch, visited_at: new Date().toISOString() },
        ...filtered,
      ].slice(0, MAX_RECENT);
      writeRecent(next);
      return next;
    });
  }, []);

  const clearRecent = useCallback(() => {
    setRecent([]);
    writeRecent([]);
  }, []);

  return { recent, trackVisit, clearRecent };
}
