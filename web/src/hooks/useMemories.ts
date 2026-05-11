import { useCallback, useEffect, useRef, useState } from "react";
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

/**
 * Atomic-facts pagination hook. Page-based — UI gets prev/next/jump
 * controls instead of an infinite scroll, which keeps a 600+ fact list
 * navigable.
 */
export function useMemories(channelId: string, limit = 25) {
  const [filters, setFilters] = useState<MemoryFilters>(defaultFilters);
  const [facts, setFacts] = useState<MemoryTier2[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [pages, setPages] = useState<number>(0);
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [fetchKey, setFetchKey] = useState(0);
  const refetch = useCallback(() => setFetchKey((k) => k + 1), []);

  // Stale-response guard. Each fetch increments the request id; only the
  // latest id's response is allowed to write state.
  const requestIdRef = useRef(0);

  const fetchPage = useCallback(
    async (pageNum: number) => {
      if (!channelId) return;
      const myId = ++requestIdRef.current;
      setIsLoading(true);

      const params = new URLSearchParams();
      params.set("page", String(pageNum));
      params.set("limit", String(limit));
      if (filters.topic) params.set("topic", filters.topic);
      if (filters.entity) params.set("entity", filters.entity);
      if (filters.minImportance) params.set("importance", filters.minImportance);

      try {
        const res = await api.get<MemoriesResponse>(
          `/api/channels/${channelId}/memories?${params.toString()}`,
        );
        if (requestIdRef.current !== myId) return;
        setFacts(res.memories);
        setTotal(res.total);
        setPages(res.pages);
        setCurrentPage(res.page);
        setError(null);
      } catch (err) {
        if (requestIdRef.current !== myId) return;
        setError(err as Error);
      } finally {
        if (requestIdRef.current === myId) setIsLoading(false);
      }
    },
    [channelId, limit, filters.topic, filters.entity, filters.minImportance],
  );

  // Reset to page 1 on filter / refetch trigger.
  useEffect(() => {
    setCurrentPage(1);
    void fetchPage(1);
  }, [fetchPage, fetchKey]);

  const goToPage = useCallback(
    (pageNum: number) => {
      if (pageNum < 1 || pageNum > pages || pageNum === currentPage) return;
      void fetchPage(pageNum);
    },
    [fetchPage, pages, currentPage],
  );

  // Stub fields kept for back-compat with callers that still destructure them.
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
    facts,
    total,
    page: currentPage,
    pages,
    pageSize: limit,
    goToPage,
    filters,
    setFilters,
    isLoading,
    error,
    refetch,
  };
}
