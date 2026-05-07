import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  EmbeddingMigrationStatus,
  EmbeddingProbeResult,
  EmbeddingSettings,
  EmbeddingUpdateRequest,
} from "@/lib/types";

export function useEmbeddingSettings() {
  const [config, setConfig] = useState<EmbeddingSettings | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchConfig = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.get<EmbeddingSettings>("/api/settings/embedding");
      setConfig(data);
    } catch (err: any) {
      setError(err?.message ?? "Failed to fetch embedding settings");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const update = useCallback(async (req: EmbeddingUpdateRequest) => {
    const data = await api.put<EmbeddingSettings>("/api/settings/embedding", req);
    setConfig(data);
    return data;
  }, []);

  const test = useCallback(async (req: EmbeddingUpdateRequest) => {
    return api.post<EmbeddingProbeResult>("/api/settings/embedding/test", req);
  }, []);

  const startMigration = useCallback(async () => {
    return api.post<{ job_id: string; status: string }>(
      "/api/settings/embedding/migrate",
      {},
    );
  }, []);

  const getMigrationStatus = useCallback(async () => {
    return api.get<EmbeddingMigrationStatus>("/api/settings/embedding/migrate/status");
  }, []);

  useEffect(() => {
    fetchConfig();
  }, [fetchConfig]);

  return {
    config,
    isLoading,
    error,
    refetch: fetchConfig,
    update,
    test,
    startMigration,
    getMigrationStatus,
  };
}
