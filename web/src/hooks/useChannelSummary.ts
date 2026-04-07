import { useState, useEffect, useCallback } from "react";
import { api, ApiError } from "@/lib/api";
import type { MemoryTier0, ChannelSummaryResponse } from "@/lib/types";

export function useChannelSummary(channelId: string) {
  const [summary, setSummary] = useState<MemoryTier0 | null>(null);
  const [clusterCount, setClusterCount] = useState(0);
  const [factCount, setFactCount] = useState(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [fetchKey, setFetchKey] = useState(0);

  const refetch = useCallback(() => setFetchKey((k) => k + 1), []);

  useEffect(() => {
    if (!channelId) {
      setIsLoading(false);
      return;
    }

    setIsLoading(true);

    api
      .get<ChannelSummaryResponse>(`/api/channels/${channelId}/summary`)
      .then((res) => {
        setSummary({
          channel_id: channelId,
          channel_name: res.channel_name || channelId,
          summary: res.text,
          description: res.description || "",
          themes: res.themes || "",
          momentum: res.momentum || "",
          team_dynamics: res.team_dynamics || "",
          updated_at: "",
          message_count: res.fact_count,
          cluster_count: res.cluster_count,
          author_count: res.author_count || 0,
          media_count: res.media_count || 0,
          worst_staleness: res.worst_staleness || 0,
          top_people: res.top_people || [],
          tech_stack: res.tech_stack || [],
          glossary_terms: res.glossary_terms || [],
          recent_activity_summary: res.recent_activity_summary || null,
        });
        setClusterCount(res.cluster_count);
        setFactCount(res.fact_count);
        setError(null);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) {
          setSummary(null);
          setError(null);
        } else {
          setError(err instanceof Error ? err : new Error(String(err)));
        }
      })
      .finally(() => setIsLoading(false));
  }, [channelId, fetchKey]);

  return { summary, clusterCount, factCount, isLoading, error, refetch };
}
