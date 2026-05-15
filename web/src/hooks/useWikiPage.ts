import { useState, useEffect, useRef } from "react";
import { api, ApiError } from "@/lib/api";
import type { WikiPage } from "@/lib/types";

/**
 * Fetches a single wiki page by ID.
 *
 * Stale-while-revalidate: the previous page content stays visible while the
 * new version is being fetched. `isLoading` is only true on the very first
 * load (no stale content to show yet). `isRevalidating` is true on every
 * subsequent background fetch so callers can apply a subtle fade if desired.
 */
export function useWikiPage(channelId: string | undefined, pageId: string | undefined, targetLang?: string) {
  const [data, setData] = useState<WikiPage | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isRevalidating, setIsRevalidating] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  // Track the last successfully rendered page key so we can skip no-op updates
  const lastKeyRef = useRef<string | null>(null);

  useEffect(() => {
    if (!channelId || !pageId) {
      setData(null);
      lastKeyRef.current = null;
      return;
    }

    const hasStale = data !== null;

    // First load → show a spinner. Subsequent loads → revalidate silently.
    if (hasStale) {
      setIsRevalidating(true);
    } else {
      setIsLoading(true);
    }

    const langParam = targetLang ? `?target_lang=${encodeURIComponent(targetLang)}` : "";
    api
      .get<WikiPage>(`/api/channels/${channelId}/wiki/pages/${pageId}${langParam}`)
      .then((res) => {
        // Only update React state when the content actually changed (last_updated guard).
        // This prevents a hard re-render — and the associated flash — when the
        // poll returns the same page state that is already on screen. The
        // ``WikiPage`` type doesn't expose ``version`` to the frontend; we use
        // ``last_updated`` (bumps on every maintainer save) as the freshness key.
        const newKey = `${channelId}/${pageId}/${res.last_updated ?? res.id}/${targetLang ?? ""}`;
        if (newKey !== lastKeyRef.current) {
          lastKeyRef.current = newKey;
          setData(res);
        }
        setError(null);
      })
      .catch((err: unknown) => {
        if (err instanceof ApiError && err.status === 404) {
          setData(null);
          lastKeyRef.current = null;
          setError(null);
        } else {
          setError(err instanceof Error ? err : new Error(String(err)));
        }
      })
      .finally(() => {
        setIsLoading(false);
        setIsRevalidating(false);
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [channelId, pageId, targetLang]);

  return { data, isLoading, isRevalidating, error };
}
