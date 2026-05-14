import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";

/** UI-facing wiki state. The bulk endpoint only returns "ready" / "empty";
 *  "building" and "errored" are layered on top by callers that have live
 *  per-channel sync state (e.g. useSync poll on the active channel). */
export type WikiState = "ready" | "building" | "empty" | "errored";

export interface WikiStateEntry {
  state: "ready" | "empty";
  last_sync_ts: string | null;
  total_synced_messages: number;
}

interface WikiStatesResponse {
  states: Record<string, WikiStateEntry>;
}

export function useWikiStates() {
  const [wikiStates, setWikiStates] = useState<Record<string, WikiStateEntry>>({});
  const [loading, setLoading] = useState(true);

  const fetchStates = useCallback(() => {
    api
      .get<WikiStatesResponse>("/api/channels/wiki-states")
      .then((data) => setWikiStates(data?.states ?? {}))
      .catch(() => setWikiStates({}))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchStates();
    const interval = setInterval(fetchStates, 30_000);
    return () => clearInterval(interval);
  }, [fetchStates]);

  const getState = useCallback(
    (channelId: string): WikiState => {
      const entry = wikiStates[channelId];
      if (!entry) return "empty";
      return entry.state;
    },
    [wikiStates],
  );

  return { wikiStates, loading, getState };
}
