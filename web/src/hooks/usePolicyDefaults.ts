import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";
import type { GlobalDefaultsResponse, SyncConfig, IngestionConfig, ConsolidationConfig, WikiConfig } from "@/lib/types";

export function usePolicyDefaults() {
  const [defaults, setDefaults] = useState<GlobalDefaultsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchDefaults = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.get<GlobalDefaultsResponse>("/api/policies/defaults");
      setDefaults(data);
    } catch (err: any) {
      setError(err.message || "Failed to fetch defaults");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const updateDefaults = useCallback(async (body: {
    sync?: SyncConfig;
    ingestion?: IngestionConfig;
    consolidation?: ConsolidationConfig;
    wiki?: WikiConfig;
    max_concurrent_syncs?: number;
  }) => {
    setError(null);
    try {
      const data = await api.put<GlobalDefaultsResponse>("/api/policies/defaults", body);
      setDefaults(data);
      return data;
    } catch (err: any) {
      setError(err.message || "Failed to update defaults");
      throw err;
    }
  }, []);

  useEffect(() => {
    fetchDefaults();
  }, [fetchDefaults]);

  return { defaults, isLoading, error, updateDefaults, refetch: fetchDefaults };
}
