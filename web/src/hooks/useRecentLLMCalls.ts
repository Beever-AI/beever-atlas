import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

/**
 * PR-λ.2: small hook for the Settings → Agent models tab to show
 * "Last call: <model> · <Xms> · <Y min ago>" inline on each agent row.
 *
 * Polls ``GET /api/settings/debug/recent-llm-calls`` every 15s while the
 * Agent Models tab is mounted. The ring buffer is process-local (50
 * entries) so this is a tiny request — no DB roundtrip, no payload bloat.
 */

export interface RecentLLMCall {
  ts: string;
  kind: "completion" | "embedding";
  consumer: string | null;
  provider: string;
  model: string;
  api_base: string | null;
  latency_ms: number | null;
  ok: boolean;
  response_model: string | null;
  error_class: string | null;
  error_summary: string | null;
}

export interface UseRecentLLMCallsResult {
  /** All recent calls, newest first. Capped at 50 by the backend. */
  calls: RecentLLMCall[];
  /** Most recent call for a given consumer, or null. */
  lastForConsumer: (consumer: string) => RecentLLMCall | null;
}

export function useRecentLLMCalls(pollMs: number = 15_000): UseRecentLLMCallsResult {
  const [calls, setCalls] = useState<RecentLLMCall[]>([]);
  const cancelledRef = useRef(false);
  const timerRef = useRef<number | undefined>(undefined);

  useEffect(() => {
    cancelledRef.current = false;
    async function poll() {
      if (cancelledRef.current) return;
      try {
        const resp = await api.get<{ calls: RecentLLMCall[] }>(
          "/api/settings/debug/recent-llm-calls",
        );
        if (!cancelledRef.current) setCalls(resp.calls ?? []);
      } catch {
        // Endpoint may be temporarily unavailable; keep last-known state.
      }
      if (!cancelledRef.current) {
        timerRef.current = window.setTimeout(poll, pollMs);
      }
    }
    poll();
    return () => {
      cancelledRef.current = true;
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [pollMs]);

  const lastForConsumer = (consumer: string): RecentLLMCall | null => {
    for (const c of calls) {
      if (c.consumer === consumer) return c;
    }
    return null;
  };

  return { calls, lastForConsumer };
}

/** Format a recency hint like "2 min ago" / "12s ago" / "just now". */
export function relativeTime(ts: string, now: Date = new Date()): string {
  const delta = (now.getTime() - new Date(ts).getTime()) / 1000;
  if (Number.isNaN(delta)) return "";
  if (delta < 5) return "just now";
  if (delta < 60) return `${Math.floor(delta)}s ago`;
  const mins = Math.floor(delta / 60);
  if (mins < 60) return `${mins} min ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  return `${Math.floor(hrs / 24)}d ago`;
}
