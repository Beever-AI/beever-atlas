import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  CreateEndpointRequest,
  DiscoverModelsResponse,
  Endpoint,
  TestConnectionResponse,
  UpdateEndpointRequest,
} from "@/lib/aiSetup";

interface EndpointListResponse {
  endpoints: Endpoint[];
}

/**
 * React hook for managing the Endpoint catalog via the
 * ``/api/settings/endpoints`` REST surface (PR-F).
 */
export function useEndpoints() {
  const [endpoints, setEndpoints] = useState<Endpoint[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.get<EndpointListResponse>("/api/settings/endpoints");
      setEndpoints(data.endpoints);
    } catch (err: any) {
      setError(err?.message || "Failed to load endpoints");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const create = useCallback(
    async (req: CreateEndpointRequest): Promise<Endpoint> => {
      const created = await api.post<Endpoint>(
        "/api/settings/endpoints",
        req
      );
      await fetchAll();
      return created;
    },
    [fetchAll]
  );

  const update = useCallback(
    async (id: string, req: UpdateEndpointRequest): Promise<Endpoint> => {
      const updated = await api.put<Endpoint>(
        `/api/settings/endpoints/${id}`,
        req
      );
      await fetchAll();
      return updated;
    },
    [fetchAll]
  );

  const remove = useCallback(
    async (id: string): Promise<void> => {
      await api.delete(`/api/settings/endpoints/${id}`);
      await fetchAll();
    },
    [fetchAll]
  );

  const test = useCallback(
    async (id: string): Promise<TestConnectionResponse> => {
      return api.post<TestConnectionResponse>(
        `/api/settings/endpoints/${id}/test`,
        {}
      );
    },
    []
  );

  const discover = useCallback(
    async (id: string): Promise<DiscoverModelsResponse> => {
      return api.post<DiscoverModelsResponse>(
        `/api/settings/endpoints/${id}/discover`,
        {}
      );
    },
    []
  );

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  return {
    endpoints,
    isLoading,
    error,
    refetch: fetchAll,
    create,
    update,
    remove,
    test,
    discover,
  };
}
