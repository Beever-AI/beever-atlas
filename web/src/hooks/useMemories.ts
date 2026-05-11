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
 * Atomic-facts pagination hook. Accumulates pages so the UI can browse the
 * full result set with a single Load more button — important when a channel
 * carries hundreds of facts but the backend caps each request at 200.
 */
export function useMemories(channelId: string, limit = 100) {
  const [filters, setFilters] = useState<MemoryFilters>(defaultFilters);
  const [facts, setFacts] = useState<MemoryTier2[]>([]);
  const [total, setTotal] = useState<number>(0);
  const [pages, setPages] = useState<number>(0);
  const [currentPage, setCurrentPage] = useState<number>(1);
  const [isLoading, setIsLoading] = useState(true);
  const [isLoadingMore, setIsLoadingMore] = useState(false);
  const [error, setError] = useState<Error | null>(null);
  const [fetchKey, setFetchKey] = useState(0);
  const refetch = useCallback(() => setFetchKey((k) => k + 1), []);

  // Stale-response guard. Each fetch increments the request id; only the
  // latest id's response is allowed to write state. Prevents a slow page-1
  // from overwriting an already-loaded page-2 (or vice versa).
  const requestIdRef = useRef(0);

  const fetchPage = useCallback(
    async (pageNum: number, append: boolean) => {
      if (!channelId) return;
      const myId = ++requestIdRef.current;
      if (append) setIsLoadingMore(true);
      else setIsLoading(true);

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
        setFacts((prev) => (append ? [...prev, ...res.memories] : res.memories));
        setTotal(res.total);
        setPages(res.pages);
        setCurrentPage(res.page);
        setError(null);
      } catch (err) {
        if (requestIdRef.current !== myId) return;
        setError(err as Error);
      } finally {
        if (requestIdRef.current === myId) {
          setIsLoading(false);
          setIsLoadingMore(false);
        }
      }
    },
    [channelId, limit, filters.topic, filters.entity, filters.minImportance],
  );

  // Reset to page 1 whenever filters or refetch trigger change.
  useEffect(() => {
    setFacts([]);
    setCurrentPage(1);
    void fetchPage(1, false);
  }, [fetchPage, fetchKey]);

  const loadMore = useCallback(() => {
    if (isLoadingMore || isLoading) return;
    if (currentPage >= pages) return;
    void fetchPage(currentPage + 1, true);
  }, [fetchPage, currentPage, pages, isLoading, isLoadingMore]);

  const hasMore = currentPage < pages;

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
    hasMore,
    loadMore,
    filters,
    setFilters,
    isLoading,
    isLoadingMore,
    error,
    refetch,
  };
}
