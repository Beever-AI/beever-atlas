import { useState, useEffect } from "react";
import { api, ApiError } from "@/lib/api";
import type { WikiPage } from "@/lib/types";

export function useWikiPage(channelId: string | undefined, pageId: string | undefined, targetLang?: string) {
  const [data, setData] = useState<WikiPage | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!channelId || !pageId) {
      setData(null);
      return;
    }

    setIsLoading(true);

    const langParam = targetLang ? `?target_lang=${encodeURIComponent(targetLang)}` : "";
    api
      .get<WikiPage>(`/api/channels/${channelId}/wiki/pages/${pageId}${langParam}`)
      .then((res) => {
        setData(res);
        setError(null);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) {
          setData(null);
          setError(null);
        } else {
          setError(err instanceof Error ? err : new Error(String(err)));
        }
      })
      .finally(() => setIsLoading(false));
  }, [channelId, pageId, targetLang]);

  return { data, isLoading, error };
}
