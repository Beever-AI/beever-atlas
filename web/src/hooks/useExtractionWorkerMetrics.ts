import { useCallback, useEffect, useRef, useState } from "react";
import { api, adminHeaders } from "@/lib/api";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface ExtractionWorkerMetrics {
  /** Per-channel queue depth (channel_id -> count). */
  queue_depth_per_channel: Record<string, number>;
  claim_rate_5min: number;
  claim_rate_15min: number;
  claim_rate_60min: number;
  success_rate_5min: number;
  /** "closed" | "open" | "half_open" */
  breaker_state: string;
  recent_failures: Array<{
    message_id: string;
    channel_id: string;
    error_class: string;
  }>;
}

export interface WikiMaintainerMetrics {
  apply_update_count_5min: number;
  mark_dirty_count_5min: number;
  rewrite_count_by_page_kind: Record<string, number>;
  pending_dirty_pages_per_channel: Record<string, number>;
  apply_update_failures: number;
}

export interface UseExtractionWorkerMetricsOptions {
  /** Poll cadence while extraction is active (pending+extracting > 0). Default 4s. */
  pollMsActive?: number;
  /** Poll cadence when idle. 0 = stop polling. Default 0. */
  pollMsIdle?: number;
  /** Whether extraction is currently active. Controls which cadence is used. */
  isActive?: boolean;
}

export interface UseExtractionWorkerMetricsReturn {
  workerMetrics: ExtractionWorkerMetrics | null;
  wikiMetrics: WikiMaintainerMetrics | null;
  loading: boolean;
  error: string | null;
  refetch: () => Promise<void>;
}

// ---------------------------------------------------------------------------
// Hook
// ---------------------------------------------------------------------------

/**
 * Polls the extraction-worker and wiki-maintainer admin metric endpoints.
 * Designed to pair with ``useExtractionStatus`` — when that hook indicates
 * pending/extracting rows, set isActive=true to enable fast polling.
 *
 * Auth: reads ``VITE_BEEVER_ADMIN_TOKEN`` via ``adminHeaders()``. If the token
 * is absent both fetches return 401 and metrics render as null (graceful).
 */
export function useExtractionWorkerMetrics(
  options: UseExtractionWorkerMetricsOptions = {},
): UseExtractionWorkerMetricsReturn {
  const { pollMsActive = 4000, pollMsIdle = 0, isActive = false } = options;

  const [workerMetrics, setWorkerMetrics] =
    useState<ExtractionWorkerMetrics | null>(null);
  const [wikiMetrics, setWikiMetrics] = useState<WikiMaintainerMetrics | null>(
    null,
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refetch = useCallback(async () => {
    setLoading(true);
    const headers = adminHeaders();
    try {
      const [worker, wiki] = await Promise.all([
        api
          .get<ExtractionWorkerMetrics>(
            "/api/admin/extraction-worker/metrics",
            { headers },
          )
          .catch(() => null),
        api
          .get<WikiMaintainerMetrics>("/api/admin/wiki-maintainer/metrics", {
            headers,
          })
          .catch(() => null),
      ]);
      setWorkerMetrics(worker);
      setWikiMetrics(wiki);
      setError(null);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to fetch worker metrics",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refetch();
    const cadence = isActive ? pollMsActive : pollMsIdle;
    if (cadence > 0) {
      intervalRef.current = setInterval(() => void refetch(), cadence);
    }
    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [isActive, pollMsActive, pollMsIdle, refetch]);

  return { workerMetrics, wikiMetrics, loading, error, refetch };
}
