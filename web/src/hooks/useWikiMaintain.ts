import { useCallback, useState } from "react";
import { api } from "@/lib/api";

interface MaintainResult {
  rewritten: number;
  errors: number;
}

export function useWikiMaintain(channelId: string | null | undefined) {
  const [result, setResult] = useState<MaintainResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const maintain = useCallback(async () => {
    if (!channelId) return;
    setLoading(true);
    setError(null);
    try {
      const r = await api.post<MaintainResult>(
        `/api/channels/${channelId}/wiki/maintain`,
      );
      setResult(r);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Maintain failed");
    } finally {
      setLoading(false);
    }
  }, [channelId]);

  return { result, loading, error, maintain };
}
