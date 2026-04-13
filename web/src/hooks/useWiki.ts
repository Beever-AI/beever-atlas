import { useState, useEffect, useCallback } from "react";
import { api, ApiError } from "@/lib/api";
import type { WikiResponse } from "@/lib/types";

export function useWiki(channelId: string | undefined, targetLang?: string) {
  const [data, setData] = useState<WikiResponse | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);
  const [isNotFound, setIsNotFound] = useState(false);
  const [fetchKey, setFetchKey] = useState(0);

  const refetch = useCallback(() => setFetchKey((k) => k + 1), []);

  useEffect(() => {
    if (!channelId) {
      setIsLoading(false);
      return;
    }

    setIsLoading(true);
    setIsNotFound(false);

    const langParam = targetLang ? `?target_lang=${encodeURIComponent(targetLang)}` : "";
    api
      .get<WikiResponse>(`/api/channels/${channelId}/wiki${langParam}`)
      .then((res) => {
        setData(res);
        setError(null);
        setIsNotFound(false);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) {
          setData(null);
          setError(null);
          setIsNotFound(true);
        } else {
          setError(err instanceof Error ? err : new Error(String(err)));
          setIsNotFound(false);
        }
      })
      .finally(() => setIsLoading(false));
  }, [channelId, targetLang, fetchKey]);

  return { data, isLoading, error, isNotFound, refetch };
}
