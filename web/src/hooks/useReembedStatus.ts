import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import type {
  EmbeddingMigrationStatus,
  EmbeddingSettings,
  EmbeddingUpdateRequest,
} from "@/lib/types";

/**
 * Re-embed migration machinery — extracted from ``useEmbeddingSettings`` so the
 * new ``EmbeddingTab`` can drive the progress UI while binding *config* to the
 * ``embedding`` Assignment instead of the legacy embedding-config API.
 *
 * ─── B-i tradeoff (documented in .omc/plans/settings-ai-tabs-restructure.md) ──
 * The ``/api/settings/embedding/migrate`` + ``/migrate/status`` endpoints AND
 * the ``GET /api/settings/embedding`` dim-mismatch read still live on the
 * *legacy* (Deprecation:-stamped) embedding API. We keep calling them here
 * because the re-embed job + dim guard + ``reembed_state`` checkpoint are real
 * backend infra that isn't going anywhere — only the *config-read/write* part
 * of the legacy embedding API is conceptually dead once Assignments own the
 * config. Re-homing ``/migrate*`` off the deprecated surface is the backend
 * follow-up (PR6, B-ii) and is intentionally out of scope here.
 *
 * ALSO B-i: ``EmbeddingTab`` must PUT the legacy ``/api/settings/embedding``
 * config (provider/model/dim) BEFORE triggering a re-embed so the legacy
 * ``/migrate`` endpoint sees the right target. That dual-write (new Assignment
 * + legacy embedding config) is exposed here as ``putLegacyConfig`` so the tab
 * doesn't reach into ``@/lib/api`` directly. KEEP THIS — see PR6 to remove.
 */

export interface PersistedEmbeddingMeta {
  provider: string;
  model: string;
  dim: number | null;
  count: number | null;
}

export interface UseReembedStatusResult {
  /** Latest migration status from ``/migrate/status`` (null until first poll). */
  status: EmbeddingMigrationStatus | null;
  /** True when SAVED legacy config differs from what's actually in storage. */
  migrationRequired: boolean;
  /** What's currently in storage (Weaviate), per the legacy embedding read. */
  persisted: PersistedEmbeddingMeta | null;
  /** Error string of the most recent *failed* migration, if any. */
  failedError: string | null;
  /** True while a migration job is running (mirror of ``status?.running``). */
  isPolling: boolean;
  /** POST ``/api/settings/embedding/migrate`` — spawn the re-embed job. */
  startMigration: () => Promise<void>;
  /** GET ``/api/settings/embedding/migrate/status`` — manual refetch. */
  refetchStatus: () => Promise<EmbeddingMigrationStatus | null>;
  /**
   * PUT ``/api/settings/embedding`` legacy config (B-i dual-write). Pass the
   * target provider/model/dim so the legacy ``/migrate`` path re-embeds toward
   * the same target the new Assignment now points at.
   */
  putLegacyConfig: (cfg: {
    provider: string;
    model: string;
    dim: number;
  }) => Promise<void>;
}

export function useReembedStatus(): UseReembedStatusResult {
  const [status, setStatus] = useState<EmbeddingMigrationStatus | null>(null);
  const [migrationRequired, setMigrationRequired] = useState(false);
  const [persisted, setPersisted] = useState<PersistedEmbeddingMeta | null>(null);

  // Keep the latest "should we still poll" decision on a ref so the poll loop's
  // ``setTimeout`` callback always sees fresh state without re-subscribing.
  const cancelledRef = useRef(false);
  const timerRef = useRef<number | undefined>(undefined);
  const lastRunningRef = useRef(false);

  const startMigration = useCallback(async () => {
    await api.post<{ job_id: string; status: string }>(
      "/api/settings/embedding/migrate",
      {},
    );
  }, []);

  const getMigrationStatus = useCallback(async () => {
    return api.get<EmbeddingMigrationStatus>(
      "/api/settings/embedding/migrate/status",
    );
  }, []);

  // Re-read the legacy embedding doc to refresh the dim-mismatch detection
  // (migration_required / persisted_*). Tolerates a missing/failed read.
  const refetchLegacy = useCallback(async () => {
    try {
      const cfg = await api.get<EmbeddingSettings>("/api/settings/embedding");
      setMigrationRequired(!!cfg.migration_required);
      setPersisted(
        cfg.persisted_provider != null && cfg.persisted_model != null
          ? {
              provider: cfg.persisted_provider,
              model: cfg.persisted_model,
              dim: cfg.persisted_dimensions,
              count: cfg.fact_count,
            }
          : null,
      );
    } catch {
      // Legacy read failed — leave the last-known state in place.
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

  const putLegacyConfig = useCallback(
    async (cfg: { provider: string; model: string; dim: number }) => {
      // B-i dual-write: keep the legacy embedding config in sync with the new
      // ``embedding`` Assignment so the legacy ``/migrate`` job (and the dim
      // guard) target the right provider/model/dim. ``confirm_migration`` is
      // set so the PUT does not error out with a dim-mismatch when we're
      // deliberately about to kick off the re-embed.
      const req: EmbeddingUpdateRequest = {
        provider: cfg.provider,
        model: cfg.model,
        dimensions: cfg.dim,
        confirm_migration: true,
      };
      await api.put<EmbeddingSettings>("/api/settings/embedding", req);
    },
    [],
  );

  // Resilient poll loop — ported verbatim from ``EmbeddingSettings``:
  //   * 2s base delay; 4s back-off on a transient error.
  //   * Never stops on transient errors / undefined results — only on an
  //     authoritative ``running: false`` response. A single 5xx mid-migration
  //     previously froze "Re-embedding · 28%" on screen forever.
  //   * On running → !running, refetch the legacy doc so the "Re-embed
  //     required" banner re-evaluates against the now-current ``embedding_meta``.
  useEffect(() => {
    cancelledRef.current = false;
    let pollUntilExplicitlyDone = true;

    async function poll() {
      if (cancelledRef.current) return;
      let nextDelay = 2000;
      try {
        const s = await getMigrationStatus();
        if (cancelledRef.current) return;
        setStatus(s);
        if (lastRunningRef.current && !s.running) {
          // Just completed (or failed). Refresh the legacy embedding doc.
          refetchLegacy();
        }
        lastRunningRef.current = s.running;
        pollUntilExplicitlyDone = s.running;
      } catch {
        // Transient — back off slightly and retry. Do NOT stop the loop.
        nextDelay = 4000;
      }
      if (!cancelledRef.current && pollUntilExplicitlyDone) {
        timerRef.current = window.setTimeout(poll, nextDelay);
      }
    }

    // Prime the dim-mismatch read once on mount, then start polling status.
    refetchLegacy();
    poll();
    return () => {
      cancelledRef.current = true;
      if (timerRef.current) window.clearTimeout(timerRef.current);
    };
  }, [getMigrationStatus, refetchLegacy]);

  const failedError =
    status && !status.running && status.error ? status.error : null;

  return {
    status,
    migrationRequired,
    persisted,
    failedError,
    isPolling: !!status?.running,
    startMigration,
    refetchStatus,
    putLegacyConfig,
  };
}
