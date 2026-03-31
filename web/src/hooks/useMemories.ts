import { useState, useEffect } from "react";
import { api } from "@/lib/api";
import type { MemoryTier2 } from "@/lib/types";

export interface MemoryFilters {
  topic: string;
  entity: string;
  minImportance: string;
  dateFrom: string;
  dateTo: string;
}

const defaultFilters: MemoryFilters = {
  topic: "",
  entity: "",
  minImportance: "",
  dateFrom: "",
  dateTo: "",
};

interface MemoriesResponse {
  memories: MemoryTier2[];
  total: number;
  page: number;
  pages: number;
}

export function useMemories(channelId: string, page = 1, limit = 50) {
  const [filters, setFilters] = useState<MemoryFilters>(defaultFilters);
  const [data, setData] = useState<MemoriesResponse>({
    memories: [],
    total: 0,
    page: 1,
    pages: 0,
  });
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!channelId) {
      setIsLoading(false);
      return;
    }

    setIsLoading(true);

    const params = new URLSearchParams();
    params.set("page", String(page));
    params.set("limit", String(limit));
    if (filters.topic) params.set("topic", filters.topic);
    if (filters.entity) params.set("entity", filters.entity);
    if (filters.minImportance) params.set("importance", filters.minImportance);

    api
      .get<MemoriesResponse>(
        `/api/channels/${channelId}/memories?${params.toString()}`,
      )
      .then((res) => {
        setData(res);
        setError(null);
      })
      .catch((err: Error) => setError(err))
      .finally(() => setIsLoading(false));
  }, [channelId, page, limit, filters.topic, filters.entity, filters.minImportance]);

  // Derive summary and clusters stubs for backward compat
  const summary = {
    channel_id: channelId,
    channel_name: channelId,
    summary: "",
    updated_at: "",
    message_count: 0,
  };

  const clusters: never[] = [];

  return {
    summary,
    clusters,
    facts: data.memories,
    total: data.total,
    page: data.page,
    pages: data.pages,
    filters,
    setFilters,
    isLoading,
    error,
  };
}
