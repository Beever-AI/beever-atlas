import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";
import type { ChannelPolicyResponse, SyncConfig, IngestionConfig, ConsolidationConfig, WikiConfig, PolicyPreset } from "@/lib/types";

export function useChannelPolicy(channelId: string | undefined) {
  const [policy, setPolicy] = useState<ChannelPolicyResponse | null>(null);
  const [presets, setPresets] = useState<PolicyPreset[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchPolicy = useCallback(async () => {
    if (!channelId) return;
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.get<ChannelPolicyResponse>(`/api/channels/${channelId}/policy`);
      setPolicy(data);
    } catch (err: any) {
      setError(err.message || "Failed to fetch policy");
    } finally {
      setIsLoading(false);
    }
  }, [channelId]);

  const fetchPresets = useCallback(async () => {
    try {
      const data = await api.get<PolicyPreset[]>("/api/policies/presets");
      setPresets(data);
    } catch {
      // presets are also available client-side as fallback
    }
  }, []);

  const savePolicy = useCallback(async (body: {
    preset?: string;
    sync?: SyncConfig;
    ingestion?: IngestionConfig;
    consolidation?: ConsolidationConfig;
    wiki?: WikiConfig;
    enabled?: boolean;
  }) => {
    if (!channelId) return;
    setError(null);
    try {
      const data = await api.put<ChannelPolicyResponse>(`/api/channels/${channelId}/policy`, body);
      setPolicy(data);
      return data;
    } catch (err: any) {
      setError(err.message || "Failed to save policy");
      throw err;
    }
  }, [channelId]);

  const deletePolicy = useCallback(async () => {
    if (!channelId) return;
    setError(null);
    try {
      await api.delete(`/api/channels/${channelId}/policy`);
      await fetchPolicy(); // refetch to get defaults
    } catch (err: any) {
      setError(err.message || "Failed to delete policy");
    }
  }, [channelId, fetchPolicy]);

  useEffect(() => {
    fetchPolicy();
    fetchPresets();
  }, [fetchPolicy, fetchPresets]);

  return { policy, presets, isLoading, error, savePolicy, deletePolicy, refetch: fetchPolicy };
}
