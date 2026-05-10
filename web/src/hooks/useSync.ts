import { useState, useEffect, useCallback, useRef } from "react";
import { api, ApiError } from "@/lib/api";
import { dedupeErrors, formatDedupedErrors } from "@/lib/dedupeErrors";
import type {
  BatchResultEntry,
  ParseFailureState,
  Phase,
  RecentEvent,
  SyncResponse,
  SyncStatusResponse,
} from "@/lib/types";

export interface SyncState {
  state: "idle" | "syncing" | "error";
  job_id?: string;
  total_messages?: number;
  parent_messages?: number;
  processed_messages?: number;
  current_batch?: number;
  total_batches?: number;
  batches_completed?: number;
  current_stage?: string | null;
  stage_timings?: Record<string, number>;
  stage_details?: {
    activity_log?: import("@/lib/types").ActivityEntry[];
    batch_stages?: Record<string, string>;
    [key: string]: unknown;
  };
  batch_results?: BatchResultEntry[];
  batch_job_state?: string | null;
  batch_job_elapsed_seconds?: number | null;
  errors?: string[];
  /** Deduped errors with per-message counts. PR-B: replaces wall-of-errors
   * with a single row per unique message. */
  dedupedErrors?: import("@/lib/dedupeErrors").DedupedError[];
  /** PR-3 — phased progress payload threaded through from
   *  ``/sync/status``. When present the renderer prefers the
   *  ``PhasedProgressCard`` over the legacy decoupled-mode widget. */
  phases?: Phase[];
  recent_events?: RecentEvent[];
  smoothed_eta_seconds?: number | null;
  retrying?: number;
  abandoned?: number;
  /** unified-llm-wiki-graph-redesign — parse-failure banner state. */
  parse_failure_state?: ParseFailureState;
}

export interface UseSyncReturn {
  syncState: SyncState;
  triggerSync: () => Promise<void>;
  isSyncing: boolean;
  error: string | null;
}

export function useSync(channelId: string, connectionId?: string | null): UseSyncReturn {
  const [syncState, setSyncState] = useState<SyncState>({ state: "idle" });
  const [isSyncing, setIsSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopPolling = useCallback(() => {
    if (intervalRef.current !== null) {
      clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const pollStatus = useCallback(async (): Promise<SyncStatusResponse | null> => {
    try {
      const status = await api.get<SyncStatusResponse>(
        `/api/channels/${channelId}/sync/status`,
      );
      // PR-B: dedupe identical errors before display so a 12-batch
      // 503 storm renders as one "(×12 batches)" row instead of a
      // wall of identical lines. The full deduped list is exposed on
      // SyncState.errors so callers can render structured rows; the
      // single-line ``error`` retains the legacy semicolon shape for
      // toast / inline-banner consumers that haven't migrated yet.
      const dedupedErrors = dedupeErrors(status.errors);
      const backendError =
        status.state === "error"
          ? formatDedupedErrors(dedupedErrors) || "Sync failed"
          : null;
      setSyncState({
        state: status.state,
        job_id: status.job_id,
        total_messages: status.total_messages,
        parent_messages: status.parent_messages,
        processed_messages: status.processed_messages,
        current_batch: status.current_batch,
        total_batches: status.total_batches,
        batches_completed: status.batches_completed,
        current_stage: status.current_stage,
        stage_timings: status.stage_timings,
        stage_details: status.stage_details,
        batch_results: status.batch_results,
        batch_job_state: status.batch_job_state,
        batch_job_elapsed_seconds: status.batch_job_elapsed_seconds,
        errors: status.errors,
        dedupedErrors,
        // PR-3 — phased progress fields (optional on legacy backends).
        phases: status.phases,
        recent_events: status.recent_events,
        smoothed_eta_seconds: status.smoothed_eta_seconds,
        retrying: status.retrying,
        abandoned: status.abandoned,
        parse_failure_state: status.parse_failure_state,
      });
      setError(backendError);
      setIsSyncing(status.state === "syncing");
      if (status.state !== "syncing") {
        stopPolling();
      }
      return status;
    } catch (err) {
      const msg = err instanceof Error ? err.message : "Failed to fetch sync status";
      setError(msg);
      stopPolling();
      setIsSyncing(false);
      setSyncState((prev) => ({ ...prev, state: "error" }));
      return null;
    }
  }, [channelId, stopPolling]);

  const startPolling = useCallback(() => {
    stopPolling();
    // Poll immediately, then every 2 seconds
    void pollStatus();
    intervalRef.current = setInterval(pollStatus, 2000);
  }, [pollStatus, stopPolling]);

  const triggerSync = useCallback(async () => {
    if (!channelId) {
      setError("Missing channel id");
      return;
    }
    if (isSyncing) return;
    setError(null);
    setIsSyncing(true);
    setSyncState({ state: "syncing" });
    try {
      // If the previous run reported no new messages, try a full resync to
      // recover from stale cursors or earlier ingestion mismatches.
      const shouldForceFullResync =
        syncState.state === "idle" &&
        !!syncState.job_id &&
        (syncState.total_messages ?? 0) === 0;
      const params = new URLSearchParams();
      if (shouldForceFullResync) params.set("sync_type", "full");
      if (connectionId) params.set("connection_id", connectionId);
      const query = params.toString();
      const syncUrl = query
        ? `/api/channels/${channelId}/sync?${query}`
        : `/api/channels/${channelId}/sync`;
      const response = await api.post<SyncResponse>(
        syncUrl,
      );
      setSyncState({
        state: "syncing",
        job_id: response.job_id,
      });
      startPolling();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // A sync is already running server-side; start polling that job.
        setError(null);
        setIsSyncing(true);
        setSyncState((prev) => ({ ...prev, state: "syncing" }));
        startPolling();
        return;
      }
      const msg = err instanceof Error ? err.message : "Sync failed";
      console.error("Sync trigger failed", { channelId, err });
      setError(msg);
      setIsSyncing(false);
      setSyncState({ state: "error" });
    }
  }, [channelId, connectionId, isSyncing, startPolling, syncState.state, syncState.job_id, syncState.total_messages]);

  useEffect(() => {
    if (!channelId) return;
    void pollStatus().then((status) => {
      if (status?.state === "syncing") {
        startPolling();
      }
    });
  }, [channelId, pollStatus, startPolling]);

  useEffect(() => {
    return () => {
      stopPolling();
    };
  }, [stopPolling]);

  return { syncState, triggerSync, isSyncing, error };
}
