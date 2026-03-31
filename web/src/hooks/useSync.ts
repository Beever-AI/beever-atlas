import { useState, useEffect, useCallback, useRef } from "react";
import { api, ApiError } from "@/lib/api";
import type { SyncResponse, SyncStatusResponse } from "@/lib/types";

export interface SyncState {
  state: "idle" | "syncing" | "error";
  job_id?: string;
  total_messages?: number;
  processed_messages?: number;
  current_batch?: number;
  current_stage?: string | null;
  stage_timings?: Record<string, number>;
  stage_details?: Record<string, Record<string, unknown>>;
  errors?: string[];
}

export interface UseSyncReturn {
  syncState: SyncState;
  triggerSync: () => Promise<void>;
  isSyncing: boolean;
  error: string | null;
}

export function useSync(channelId: string): UseSyncReturn {
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
      const backendError =
        status.state === "error"
          ? status.errors?.filter(Boolean).join("; ") || "Sync failed"
          : null;
      setSyncState({
        state: status.state,
        job_id: status.job_id,
        total_messages: status.total_messages,
        processed_messages: status.processed_messages,
        current_batch: status.current_batch,
        current_stage: status.current_stage,
        stage_timings: status.stage_timings,
        stage_details: status.stage_details,
        errors: status.errors,
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
    // Poll immediately, then every 5 seconds
    void pollStatus();
    intervalRef.current = setInterval(pollStatus, 5000);
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
      const syncUrl = shouldForceFullResync
        ? `/api/channels/${channelId}/sync?sync_type=full`
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
  }, [channelId, isSyncing, startPolling, syncState.state, syncState.job_id, syncState.total_messages]);

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
