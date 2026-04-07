import { useState, useEffect, useCallback } from "react";
import { api, ApiError } from "@/lib/api";
import type { TopicCluster, MemoryTier1 } from "@/lib/types";

export function useTopics(channelId: string) {
  const [clusters, setClusters] = useState<MemoryTier1[]>([]);
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
      .get<TopicCluster[]>(`/api/channels/${channelId}/topics`)
      .then((res) => {
        const mapped: MemoryTier1[] = res.map((c) => ({
          id: c.id,
          title: c.title || "",
          topic: c.title || c.summary?.split(".")[0] || "Untitled topic",
          summary: c.summary,
          current_state: c.current_state || "",
          open_questions: c.open_questions || "",
          impact_note: c.impact_note || "",
          fact_count: c.member_count,
          date_range: { start: c.date_range_start || "", end: c.date_range_end || "" },
          topic_tags: c.topic_tags,
          authors: c.authors || [],
          status: c.status || "active",
          staleness_score: c.staleness_score || 0,
          key_facts: c.key_facts || [],
          people: c.people || [],
          decisions: c.decisions || [],
          technologies: c.technologies || [],
          faq_candidates: c.faq_candidates || [],
          fact_type_counts: c.fact_type_counts || {},
        }));
        setClusters(mapped);
        setError(null);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) {
          setClusters([]);
          setError(null);
        } else {
          setError(err instanceof Error ? err : new Error(String(err)));
        }
      })
      .finally(() => setIsLoading(false));
  }, [channelId, fetchKey]);

  return { clusters, isLoading, error, refetch };
}
