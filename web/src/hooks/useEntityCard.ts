import { useState, useEffect, useRef } from "react";
import { api, ApiError } from "@/lib/api";

/**
 * Shape of the `/api/entities/{name}/card` response (see
 * `src/beever_atlas/api/topics.py:232-258`'s EntityCardResponse).
 * Mirrored here so the FE doesn't import server-side schemas.
 */
export interface EntityCardData {
  entity_name: string;
  entity_type: string;
  channel_ids: string[];
  cluster_ids: string[];
  fact_count: number;
  fact_type_breakdown: Record<string, number>;
  key_facts: string[];
  related_entities: Array<{ name?: string; type?: string; [key: string]: unknown }>;
  last_mentioned_at: string;
  staleness_score: number;
  summary: string;
}

interface UseEntityCardReturn {
  card: EntityCardData | null;
  loading: boolean;
  /** "not_found" indicates the 404 empty-state branch; other errors keep
   *  their server message. */
  error: string | null;
  notFound: boolean;
}

/**
 * Fetch the workspace-wide knowledge card for an entity. Skips when
 * `entityName` is null (panel closed) and memoizes per-name to avoid
 * refetching on tab toggles / rerenders.
 */
export function useEntityCard(entityName: string | null): UseEntityCardReturn {
  const [card, setCard] = useState<EntityCardData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const cacheRef = useRef<Map<string, EntityCardData>>(new Map());

  useEffect(() => {
    if (!entityName) {
      setCard(null);
      setError(null);
      setNotFound(false);
      setLoading(false);
      return;
    }

    const cached = cacheRef.current.get(entityName);
    if (cached) {
      setCard(cached);
      setError(null);
      setNotFound(false);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    setError(null);
    setNotFound(false);

    api
      .get<EntityCardData>(`/api/entities/${encodeURIComponent(entityName)}/card`)
      .then((data) => {
        if (cancelled) return;
        cacheRef.current.set(entityName, data);
        setCard(data);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 404) {
          setNotFound(true);
          setCard(null);
        } else {
          setError(err instanceof Error ? err.message : "Failed to load entity card");
          setCard(null);
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [entityName]);

  return { card, loading, error, notFound };
}
