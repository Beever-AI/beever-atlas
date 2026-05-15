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
  /** The ``job_id`` from the most-recent ``/sync/status`` response.
   *  This is whichever row the server returned — possibly a previous
   *  run's row during the brief window after trigger but before the new
   *  row lands. Use ``triggered_job_id`` for the source-of-truth of
   *  "what sync did the user actually start?". */
  job_id?: string;
  /** The ``job_id`` returned by ``POST /sync`` (the trigger). This is
   *  the authoritative id for the active sync from the caller's
   *  perspective — SyncProgressV2 gates its ``batch_results`` ingestion
   *  on (``job_id === triggered_job_id``) so that stale rows from the
   *  previous run can't leak DONE chips into the new view. */
  triggered_job_id?: string;
  total_messages?: number;
  parent_messages?: number;
  processed_messages?: number;
  total_batches?: number;
  batches_completed?: number;
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
  /** ISO timestamp of when the current sync job started — used by
   *  SyncProgressV2 to compute elapsed-time stamps in the activity feed
   *  (e.g. "0:42", "1:14") instead of noisy relative time ("30s ago"). */
  started_at?: string | null;
}

export interface UseSyncReturn {
  syncState: SyncState;
  triggerSync: () => Promise<void>;
  isSyncing: boolean;
  error: string | null;
}

/** Derive the active pipeline phase from the API response.
 *  Used to (a) pick the next poll interval and (b) by the dedup guard. */
function derivePhase(
  status: SyncStatusResponse,
): "syncing" | "extracting" | "building" | "done" | "error" {
  if (status.state === "error") return "error";
  const phases = status.phases ?? [];
  const byName = (name: string) => phases.find((p) => p.name === name);
  if (byName("fetched")?.state === "in_flight" || status.state === "syncing") {
    return "syncing";
  }
  if (byName("extracting")?.state === "in_flight") return "extracting";
  if (
    byName("wiki_maintenance")?.state === "in_flight" ||
    byName("overview_wiki")?.state === "in_flight"
  ) {
    return "building";
  }
  return "done";
}

/** Cheap fingerprint over the fields whose changes warrant a re-render.
 *  Avoids ``JSON.stringify`` to keep this O(N) in event count. */
function fingerprintStatus(status: SyncStatusResponse): string {
  const phaseStates = (status.phases ?? []).map((p) => `${p.name}:${p.state}`).join(",");
  const evCount = (status.recent_events ?? []).length;
  const lastEvTs = (status.recent_events ?? [])[0]?.ts ?? "";
  const parseFails = status.parse_failure_state?.count_last_10_min ?? 0;
  // sync-monitor-redesign — include activity_log signal so the
  // MetricsBar / per-batch tabs re-render as the worker pushes new
  // stage_output entries. Without this, the dedup guard would skip
  // re-renders when only the activity_log changes (no message_processing
  // bump, no phase transition).
  const activityLog =
    (status.stage_details as { activity_log?: unknown[] } | undefined)
      ?.activity_log ?? [];
  const logCount = activityLog.length;
  const lastLogKey = (() => {
    const last = activityLog[logCount - 1] as
      | { batch_idx?: number; agent?: string; type?: string }
      | undefined;
    if (!last) return "";
    return `${last.batch_idx ?? "-"}:${last.agent ?? "-"}:${last.type ?? "-"}`;
  })();
  return [
    status.state,
    status.processed_messages ?? -1,
    status.total_messages ?? -1,
    phaseStates,
    evCount,
    lastEvTs,
    parseFails,
    logCount,
    lastLogKey,
  ].join("|");
}

export function useSync(channelId: string, connectionId?: string | null): UseSyncReturn {
  const [syncState, setSyncState] = useState<SyncState>({ state: "idle" });
  const [isSyncing, setIsSyncing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // setTimeout chain instead of setInterval so the next poll can pick a
  // phase-appropriate delay (2s during extract, 3s during build, stop on done).
  const timeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Fingerprint of the most-recent response — skip setSyncState when
  // identical to avoid re-render thrash on each poll tick.
  const lastFingerprintRef = useRef<string>("");

  const stopPolling = useCallback(() => {
    if (timeoutRef.current !== null) {
      clearTimeout(timeoutRef.current);
      timeoutRef.current = null;
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
      // Dedup guard — skip the React re-render when nothing material
      // changed since the last poll. Eliminates the flicker that was
      // surfacing during the SyncMonitor live updates.
      const fp = fingerprintStatus(status);
      if (fp !== lastFingerprintRef.current) {
        lastFingerprintRef.current = fp;
        setSyncState((prev) => ({
          // Preserve ``triggered_job_id`` across polls — only the
          // trigger sets it. SyncProgressV2 uses it as the canonical
          // current-sync id to gate against stale ``batch_results``.
          triggered_job_id: prev.triggered_job_id,
          state: status.state,
          job_id: status.job_id,
          total_messages: status.total_messages,
          parent_messages: status.parent_messages,
          processed_messages: status.processed_messages,
          total_batches: status.total_batches,
          batches_completed: status.batches_completed,
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
          // sync-monitor-redesign — surface started_at so the activity
          // feed can compute elapsed-time stamps.
          started_at: status.started_at,
        }));
        setError(backendError);
      }
      // isSyncing reflects active fetch only — extraction can still be
      // flushing after state flips to ``idle``, but the SyncMonitor
      // detects that via the phase waterfall.
      setIsSyncing(status.state === "syncing");
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
    // Phase-aware adaptive cadence — setTimeout chain so each tick picks
    // its own delay based on the response's active phase.
    //   syncing / extracting: 2s
    //   building wiki:        3s
    //   done:                 stop (the next user action triggers a fresh poll)
    //   error:                stop
    const tick = async () => {
      const status = await pollStatus();
      if (!status) return;
      const phase = derivePhase(status);
      if (phase === "done" || phase === "error") {
        // Still let the activity feed receive the last frame; no further polls.
        return;
      }
      const delay = phase === "building" ? 3000 : 2000;
      timeoutRef.current = setTimeout(() => void tick(), delay);
    };
    void tick();
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
      // Reset the fingerprint so the very next poll re-renders even
      // though state is also "syncing" — without this, the dedup guard
      // could swallow the first fresh-job response and leave the UI
      // showing the trigger's optimistic snapshot.
      lastFingerprintRef.current = "";
      setSyncState({
        state: "syncing",
        job_id: response.job_id,
        // ``triggered_job_id`` is the authoritative current-sync id —
        // SyncProgressV2 uses it to gate ``batch_results`` ingestion
        // against stale rows that ``/sync/status`` may briefly return
        // before the new ``sync_jobs`` row lands.
        triggered_job_id: response.job_id,
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
      if (!status) return;
      // Start polling whenever there's live pipeline activity. Under the
      // decoupled flow the sync HTTP returns immediately (state = idle)
      // but the background worker keeps processing, so we must keep
      // polling as long as ANY phase is still in_flight — otherwise the
      // UI shows a frozen snapshot and the user has to hard-refresh.
      const phasesInFlight = (status.phases ?? []).some(
        (p) => p.state === "in_flight",
      );
      if (status.state === "syncing" || phasesInFlight) {
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
