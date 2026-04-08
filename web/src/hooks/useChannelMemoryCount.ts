import { useEffect, useState } from "react";
import { api } from "@/lib/api";

interface MemoriesCountResponse {
  total: number;
}

export function useChannelMemoryCount(channelId: string | undefined) {
  const [memoryCount, setMemoryCount] = useState<number>(0);
  const [isLoading, setIsLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    if (!channelId) {
      setMemoryCount(0);
      setIsLoading(false);
      return;
    }

    setIsLoading(true);

    api
      .get<MemoriesCountResponse>(`/api/channels/${channelId}/memories?page=1&limit=1`)
      .then((res) => {
        setMemoryCount(res.total ?? 0);
        setError(null);
      })
      .catch((err: unknown) => {
        setMemoryCount(0);
        setError(err instanceof Error ? err : new Error(String(err)));
      })
      .finally(() => setIsLoading(false));
  }, [channelId]);

  return {
    memoryCount,
    hasMemories: memoryCount > 0,
    isLoading,
    error,
  };
}
