import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";

/**
 * Per-status counts returned by ``GET /api/channels/{id}/extraction-status``.
 * Always zero-filled for missing statuses (PR-B.4 server contract) so the UI
 * can render a stable progress bar without conditionals.
 */
export interface ExtractionStatusCounts {
  pending: number;
  extracting: number;
  done: number;
  failed: number;
}

export interface ExtractionStatusResponse {
  channel_id: string;
  counts: ExtractionStatusCounts;
  total: number;
}

export interface UseExtractionStatusOptions {
  /** Poll cadence while a sync is running. Default: 5s. */
  pollMsActive?: number;
  /** Poll cadence when sync is idle (still useful for in-flight worker
   * batches that finish after sync returned). Default: 30s. Set to 0 to
   * stop polling entirely when the channel is idle. */
  pollMsIdle?: number;
  /** Whether a sync is currently running — flips polling cadence between
   * active and idle. */
  isSyncing?: boolean;
}

export interface UseExtractionStatusReturn {
  status: ExtractionStatusResponse | null;
  loading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
}

/**
 * Polls the extraction-status endpoint so the UI can show
 * "Enriching: X of Y messages complete" as a deduped replacement for
 * the wall-of-503 banner. Designed to coexist with ``useSync`` —
 * cadence flips to 5s while syncing, 30s otherwise.
 */
export function useExtractionStatus(
  channelId: string | null | undefined,
  options: UseExtractionStatusOptions = {},
): UseExtractionStatusReturn {
  const { pollMsActive = 5000, pollMsIdle = 30000, isSyncing = false } = options;
  const [status, setStatus] = useState<ExtractionStatusResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  // Last response signature — used to skip ``setStatus`` when the
  // counts are identical so sibling-hook re-renders don't trigger a
  // wiki-page opacity flash on every 30s idle poll. Mirrors the
  // ``last_updated`` guard in ``useWikiPage`` from commit c23c955 but
  // applied per-field rather than over the full payload (the backend
  // may add timestamp/etag fields later that would defeat a
  // ``JSON.stringify`` guard — see plan §"What NOT to do" item 5).
  const lastKeyRef = useRef<string | null>(null);

  const refetch = useCallback(async () => {
    if (!channelId) return;
    setLoading(true);
    try {
      const resp = await api.get<ExtractionStatusResponse>(
        `/api/channels/${channelId}/extraction-status`,
      );
      const key = `${resp.total}|${resp.counts.pending}|${resp.counts.extracting}|${resp.counts.done}|${resp.counts.failed}`;
      if (key !== lastKeyRef.current) {
        lastKeyRef.current = key;
        setStatus(resp);
      }
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to fetch extraction status");
    } finally {
      setLoading(false);
    }
  }, [channelId]);

  useEffect(() => {
    if (!channelId) {
      setStatus(null);
      lastKeyRef.current = null;
      return;
    }
    // Reset the dedup key when the channel changes so the first
    // response for the new channel is always pushed to state, even if
    // its counts happen to match the previous channel's last response.
    lastKeyRef.current = null;
    void refetch();
    const cadence = isSyncing ? pollMsActive : pollMsIdle;
    if (cadence > 0) {
      intervalRef.current = setInterval(() => void refetch(), cadence);
    }
    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [channelId, isSyncing, pollMsActive, pollMsIdle, refetch]);

  return { status, loading, error, refetch };
}
