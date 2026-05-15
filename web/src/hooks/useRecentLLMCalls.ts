import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";

/**
 * PR-λ.2: small hook for the Settings → Agent models tab to show
 * "Last call: <model> · <Xms> · <Y min ago>" inline on each agent row.
 *
 * Polls ``GET /api/settings/debug/recent-llm-calls`` every 15s while the
 * Agent Models tab is mounted. The ring buffer is process-local (50
 * entries) so this is a tiny request — no DB roundtrip, no payload bloat.
 *
 * Stale-tab guard: a tab opened against a previous build that has no
 * ``VITE_BEEVER_API_KEY`` baked in will hit ``401`` on every poll and
 * spam the backend log forever. After ``MAX_AUTH_FAILS`` consecutive
 * ``401``/``403`` responses the hook stops polling entirely until the
 * tab reloads. Non-auth failures (network blip, 5xx) keep the normal
 * cadence so transient issues self-heal.
 */
const MAX_AUTH_FAILS = 2;

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
  /**
   * Most recent call attributable to a given (api_base, model) tuple.
   * Catches the qa_agent path which goes via Google ADK's ``LiteLlm``
   * wrapper — the LiteLLM callback can't see the consumer name, so we
   * match by what the call carries on the wire.
   */
  lastByModel: (apiBase: string | null | undefined, model: string) => RecentLLMCall | null;
}

export function useRecentLLMCalls(pollMs: number = 15_000): UseRecentLLMCallsResult {
  const [calls, setCalls] = useState<RecentLLMCall[]>([]);
  const cancelledRef = useRef(false);
  const timerRef = useRef<number | undefined>(undefined);
  const authFailsRef = useRef(0);

  useEffect(() => {
    cancelledRef.current = false;
    authFailsRef.current = 0;
    async function poll() {
      if (cancelledRef.current) return;
      try {
        const resp = await api.get<{ calls: RecentLLMCall[] }>(
          "/api/settings/debug/recent-llm-calls",
        );
        if (!cancelledRef.current) setCalls(resp.calls ?? []);
        authFailsRef.current = 0;
      } catch (err) {
        // Endpoint may be temporarily unavailable; keep last-known state.
        // Persistent 401/403 means the bundle's baked-in key no longer
        // matches the server's ``BEEVER_API_KEYS`` (typical after an env
        // rotation + web rebuild — every stale tab spams 401 forever).
        // Stop polling after a couple of failures so a forgotten tab
        // doesn't pollute server logs; a hard reload resets the counter.
        if (err instanceof ApiError && (err.status === 401 || err.status === 403)) {
          authFailsRef.current += 1;
          if (authFailsRef.current >= MAX_AUTH_FAILS) {
            return;
          }
        }
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

  const lastByModel = (
    apiBase: string | null | undefined,
    model: string,
  ): RecentLLMCall | null => {
    if (!model) return null;
    const targetBase = (apiBase ?? "").replace(/\/+$/, "");
    for (const c of calls) {
      // Match the bare model id (LiteLLM may strip an explicit ``<provider>/``
      // prefix during dispatch — we record what hit the wire).
      const bare = c.model.includes("/") ? c.model.split("/", 2)[1] : c.model;
      if (bare !== model) continue;
      const callBase = (c.api_base ?? "").replace(/\/+$/, "");
      if (apiBase && callBase && callBase !== targetBase) continue;
      return c;
    }
    return null;
  };

  return { calls, lastForConsumer, lastByModel };
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
