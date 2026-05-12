import { useCallback, useEffect, useState } from "react";
import { api } from "@/lib/api";
import type {
  Assignment,
  AssignmentListResponse,
  PresetResponse,
  UpdateAssignmentRequest,
} from "@/lib/aiSetup";

/**
 * React hook for managing per-consumer LLM assignments via the
 * ``/api/settings/assignments`` REST surface (PR-F).
 */
export function useAssignments() {
  const [assignments, setAssignments] = useState<Assignment[]>([]);
  const [defaultConsumers, setDefaultConsumers] = useState<string[]>([]);
  const [capabilities, setCapabilities] = useState<Record<string, string[]>>({});
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchAll = useCallback(async () => {
    setIsLoading(true);
    setError(null);
    try {
      const data = await api.get<AssignmentListResponse>(
        "/api/settings/assignments"
      );
      setAssignments(data.assignments);
      setDefaultConsumers(data.default_consumers);
      setCapabilities(data.capabilities);
    } catch (err: any) {
      setError(err?.message || "Failed to load assignments");
    } finally {
      setIsLoading(false);
    }
  }, []);

  const upsert = useCallback(
    async (consumer: string, req: UpdateAssignmentRequest): Promise<Assignment> => {
      const saved = await api.put<Assignment>(
        `/api/settings/assignments/${consumer}`,
        req
      );
      await fetchAll();
      return saved;
    },
    [fetchAll]
  );

  const remove = useCallback(
    async (consumer: string): Promise<void> => {
      await api.delete(`/api/settings/assignments/${consumer}`);
      await fetchAll();
    },
    [fetchAll]
  );

  const previewPreset = useCallback(
    async (preset: string): Promise<PresetResponse> => {
      return api.post<PresetResponse>("/api/settings/assignments/preset", {
        preset,
        confirm: false,
      });
    },
    []
  );

  const applyPreset = useCallback(
    async (
      preset: string,
      force_overwrite_custom: boolean = false
    ): Promise<PresetResponse> => {
      const result = await api.post<PresetResponse>(
        "/api/settings/assignments/preset",
        { preset, confirm: true, force_overwrite_custom }
      );
      await fetchAll();
      return result;
    },
    [fetchAll]
  );

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  return {
    assignments,
    defaultConsumers,
    capabilities,
    isLoading,
    error,
    refetch: fetchAll,
    upsert,
    remove,
    previewPreset,
    applyPreset,
  };
}
