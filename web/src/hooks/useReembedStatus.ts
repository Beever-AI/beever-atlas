import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type {
  EmbeddingMigrationStatus,
  EmbeddingReembedSpawnResponse,
  EmbeddingReembedState,
} from "@/lib/types";

/**
 * Re-embed migration machinery — extracted from ``useEmbeddingSettings`` so the
 * ``EmbeddingTab`` can drive the progress UI while binding *config* to the
 * ``embedding`` Assignment instead of the legacy embedding-config API.
 *
 * ─── B-i re-home: PR6 landed ──────────────────────────────────────────────
 * This hook now talks exclusively to the **non-deprecated**
 * ``/api/settings/embedding-migration/*`` surface:
 *   * ``POST /spawn``  — server-side dual-writes the ``embedding`` Assignment's
 *     config into the legacy ``embedding_settings`` doc + credential, THEN
 *     spawns the re-embed job. (The frontend used to do that dual-write
 *     itself via a legacy ``PUT /api/settings/embedding`` — that's gone.)
 *   * ``GET /status`` — current re-embed job state.
 *   * ``GET /state``  — dim-mismatch detection (desired vs persisted) + a
 *     ``reembed_supported`` flag derived from the Assignment's endpoint.
 *
 * The only remaining legacy coupling is *server-side and invisible to this
 * hook*: the re-embed *job* still reads its target from the
 * ``embedding_settings`` Mongo doc (which ``/spawn`` populates) until the
 * embedding *runtime* itself reads Assignments directly — a separate future
 * change. Nothing here hits a Deprecation:-stamped route anymore.
 */

export interface PersistedEmbeddingMeta {
  provider: string;
  model: string;
  dim: number | null;
  count: number | null;
}

export interface UseReembedStatusResult {
  /** Latest migration status from ``/status`` (null until first poll). */
  status: EmbeddingMigrationStatus | null;
  /** True when the desired (Assignment) config differs from what's in storage. */
  migrationRequired: boolean;
  /** What's currently in storage (Weaviate), per ``GET /state``'s persisted_*. */
  persisted: PersistedEmbeddingMeta | null;
  /** Error string of the most recent *failed* migration, if any. */
  failedError: string | null;
  /** True while a migration job is running (mirror of ``status?.running``). */
  isPolling: boolean;
  /**
   * True when the Assignment's endpoint resolves to a provider the re-embed
   * job can drive. False ⇒ the "Start re-embed" action should be disabled.
   * Defaults to ``true`` until the first ``/state`` read resolves so the UI
   * doesn't flash a disabled state on mount.
   */
  reembedSupported: boolean;
  /** When ``reembedSupported`` is false, the backend's explanation. */
  reembedSupportReason: string | null;
  /** POST ``/api/settings/embedding-migration/spawn`` — spawn the re-embed job. */
  startMigration: () => Promise<void>;
  /** GET ``/api/settings/embedding-migration/status`` — manual refetch. */
  refetchStatus: () => Promise<EmbeddingMigrationStatus | null>;
}

export function useReembedStatus(): UseReembedStatusResult {
  const [status, setStatus] = useState<EmbeddingMigrationStatus | null>(null);
  const [migrationRequired, setMigrationRequired] = useState(false);
  const [persisted, setPersisted] = useState<PersistedEmbeddingMeta | null>(null);
  const [reembedSupported, setReembedSupported] = useState(true);
  const [reembedSupportReason, setReembedSupportReason] = useState<string | null>(null);

  // Keep the latest "should we still poll" decision on a ref so the poll loop's
  // ``setTimeout`` callback always sees fresh state without re-subscribing.
  const cancelledRef = useRef(false);
  const timerRef = useRef<number | undefined>(undefined);
  const lastRunningRef = useRef(false);

  const startMigration = useCallback(async () => {
    // The /spawn endpoint does the server-side dual-write (Assignment config →
    // legacy embedding_settings doc + credential) then spawns the job. Errors
    // (e.g. 422 unsupported_embedding_provider_for_reembed) surface via the
    // thrown ApiError so callers can show them.
    await api.post<EmbeddingReembedSpawnResponse>(
      "/api/settings/embedding-migration/spawn",
      {},
    );
  }, []);

  const getMigrationStatus = useCallback(async () => {
    return api.get<EmbeddingMigrationStatus>(
      "/api/settings/embedding-migration/status",
    );
  }, []);

  // Re-read the dim-mismatch state (migration_required / persisted_* /
  // reembed_supported). Tolerates a missing/failed read.
  const refetchState = useCallback(async () => {
    try {
      const s = await api.get<EmbeddingReembedState>(
        "/api/settings/embedding-migration/state",
      );
      setMigrationRequired(!!s.migration_required);
      setReembedSupported(!!s.reembed_supported);
      setReembedSupportReason(s.reason ?? null);
      setPersisted(
        s.persisted_provider != null && s.persisted_model != null
          ? {
              provider: s.persisted_provider,
              model: s.persisted_model,
              dim: s.persisted_dimensions,
              count: s.fact_count,
            }
          : null,
      );
    } catch {
      // State read failed — leave the last-known state in place.
    }
  }, []);

  const refetchStatus = useCallback(async (): Promise<EmbeddingMigrationStatus | null> => {
    try {
      const s = await getMigrationStatus();
      setStatus(s);
      lastRunningRef.current = s.running;
      return s;
    } catch {
      return null;
    }
  }, [getMigrationStatus]);

  // Resilient poll loop — PR-θ:
  //   * Always polling while the component is mounted (was: only while a
  //     job was running). The previous behaviour exited the loop on the
  //     FIRST ``running: false`` response, which is exactly what happens
  //     on mount before the user has clicked Start re-embed — so the
  //     subsequent click spawned a migration that the UI never saw.
  //     Progress bar would stay at "0 / 0 facts · starting" forever even
  //     while the backend was finishing the migration. Always-poll is
  //     ~1 tiny Mongo ``find_one`` every 2-10s and keeps the UI honest.
  //   * 2s base delay while a job is running (responsive progress bar);
  //     8s when idle (mostly a no-op poll just to stay synced).
  //   * 4s back-off on transient errors. Never stops on errors — a single
  //     5xx mid-migration previously froze "Re-embedding · 28%" on screen.
  //   * On running → !running, refetch the state doc so the "Re-embed
  //     required" banner re-evaluates against the now-current ``embedding_meta``.
  useEffect(() => {
    cancelledRef.current = false;

    async function poll() {
      if (cancelledRef.current) return;
      let nextDelay = 2000;
      try {
        const s = await getMigrationStatus();
        if (cancelledRef.current) return;
        setStatus(s);
        if (lastRunningRef.current && !s.running) {
          // Just completed (or failed). Refresh the state doc.
          refetchState();
        }
        lastRunningRef.current = s.running;
        // Slower poll when no job is running — the UI just needs to notice
        // if a migration starts (e.g. from another tab) or finishes.
        nextDelay = s.running ? 2000 : 8000;
      } catch {
        // Transient — back off slightly and retry. Do NOT stop the loop.
        nextDelay = 4000;
      }
      if (!cancelledRef.current) {
        timerRef.current = window.setTimeout(poll, nextDelay);
      }
    }

    // Prime the dim-mismatch read once on mount, then start polling status.
    refetchState();
    poll();
    return () => {
      cancelledRef.current = true;
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [getMigrationStatus, refetchState]);

  const failedError =
    status && !status.running && status.error ? status.error : null;

  return {
    status,
    migrationRequired,
    persisted,
    failedError,
    isPolling: !!status?.running,
    reembedSupported,
    reembedSupportReason,
    startMigration,
    refetchStatus,
  };
}
