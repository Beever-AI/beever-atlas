import { useState, useCallback, useRef, useEffect } from "react";
import { api } from "@/lib/api";

export interface WikiGenerationStatus {
  status: "idle" | "running" | "done" | "failed";
  channel_id?: string;
  stage?: string;
  stage_detail?: string;
  pages_total?: number;
  pages_done?: number;
  pages_completed?: string[];
  model?: string;
  error?: string | null;
  started_at?: string;
  updated_at?: string;
}

export function useWikiRefresh(channelId: string | undefined, targetLang?: string) {
  const [isPending, setIsPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [generationStatus, setGenerationStatus] =
    useState<WikiGenerationStatus | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const onDoneRef = useRef<(() => void) | null>(null);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  // Clean up on unmount
  useEffect(() => stopPolling, [stopPolling]);

  const consecutiveFailuresRef = useRef(0);

  const pollStatus = useCallback(async () => {
    if (!channelId) return;
    try {
      const langParam = targetLang ? `?target_lang=${encodeURIComponent(targetLang)}` : "";
      const status = await api.get<WikiGenerationStatus>(
        `/api/channels/${channelId}/wiki/status${langParam}`
      );
      consecutiveFailuresRef.current = 0;
      setGenerationStatus(status);

      if (status.status === "done") {
        stopPolling();
        setIsPending(false);
        // Notify caller to refetch wiki
        onDoneRef.current?.();
      } else if (status.status === "failed") {
        stopPolling();
        setIsPending(false);
        setError(new Error(status.error || "Wiki generation failed"));
      }
    } catch {
      consecutiveFailuresRef.current += 1;
      if (consecutiveFailuresRef.current >= 10) {
        stopPolling();
        setIsPending(false);
        setError(new Error("Lost connection to wiki refresh; please retry."));
      }
    }
  }, [channelId, targetLang, stopPolling]);

  const mutate = useCallback(
    async (onDone?: () => void) => {
      if (!channelId) return;
      // Reset circuit breaker + prior error so a user-initiated retry after
      // 10 consecutive polling failures doesn't immediately re-trip.
      consecutiveFailuresRef.current = 0;
      setIsPending(true);
      setError(null);
      setGenerationStatus({
        status: "running",
        stage: "starting",
        stage_detail: "Initiating wiki generation…",
      });
      onDoneRef.current = onDone ?? null;

      try {
        const langParam = targetLang ? `?target_lang=${encodeURIComponent(targetLang)}` : "";
        await api.post(`/api/channels/${channelId}/wiki/refresh${langParam}`);
        // Start polling for status
        stopPolling();
        pollRef.current = setInterval(pollStatus, 2000);
        // Immediate first poll
        pollStatus();
      } catch (err) {
        setError(err instanceof Error ? err : new Error(String(err)));
        setIsPending(false);
        setGenerationStatus(null);
      }
    },
    [channelId, targetLang, pollStatus, stopPolling]
  );

  return { mutate, isPending, error, generationStatus };
}
